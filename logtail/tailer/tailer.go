// Package tailer implements a log file follower with production-grade rotation handling.
//
// Rotation strategies supported:
//   - rename + create (logrotate default): drain old inode to EOF, re-open path.
//   - copytruncate: detect size < offset, reset offset to 0 on same inode.
//   - remove + recreate: re-open when file reappears.
//   - symlink target change: resolve path each poll; detect inode change.
//
// Detection uses a hybrid approach:
//   - fsnotify watcher on parent directory (for create/rename events).
//   - fsnotify watcher on the file itself (for write events — low-latency new data).
//   - Periodic stat polling as fallback (handles copytruncate, symlink changes,
//     and missed fsnotify events).
package tailer

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"github.com/fsnotify/fsnotify"

	"github.com/Yanghh0220/loggazer/logtail/checkpoint"
	"github.com/Yanghh0220/loggazer/logtail/identity"
)

// Config holds configuration for a single file follower.
type Config struct {
	// Path is the absolute or relative path to the log file.
	Path string

	// PollInterval is the fallback stat polling interval.
	// Default: 5s.
	PollInterval time.Duration

	// FlushInterval is how often to flush the checkpoint to disk.
	// Default: 5s.
	FlushInterval time.Duration

	// ReopenTimeout is how long to wait for a rotated file to reappear
	// before giving up (0 = retry indefinitely).
	// Default: 60s.
	ReopenTimeout time.Duration

	// MaxLineSize is the maximum line length in bytes. 0 means 64KB.
	MaxLineSize int

	// StartOffset controls where to begin when no checkpoint is found:
	//   0  = beginning of file
	//   -1 = end of file (tail -f behavior)
	StartOffset int64
}

// Line represents one log line with its source path and byte offset.
type Line struct {
	Path   string
	Offset int64
	Text   string
}

// Tailer follows a single log file, emitting lines to a channel.
// It handles rotation transparently: consumers see an uninterrupted stream.
type Tailer struct {
	cfg   Config
	lines chan Line
	store *checkpoint.Store

	// State protected by mu.
	mu        sync.Mutex
	f         *os.File
	fileId    identity.FileId
	offset    int64 // byte offset of next unread byte
	reader    *bufio.Reader
	lastFlush time.Time

	// Shutdown coordination.
	ctx    context.Context
	cancel context.CancelFunc
	done   chan struct{}

	// Stats (atomic for lock-free access).
	rotations atomic.Int64
	bytesRead atomic.Int64
	errors    atomic.Int64
}

// New creates a new Tailer. Call Run to start following.
func New(cfg Config, store *checkpoint.Store) *Tailer {
	if cfg.PollInterval <= 0 {
		cfg.PollInterval = 5 * time.Second
	}
	if cfg.FlushInterval <= 0 {
		cfg.FlushInterval = 5 * time.Second
	}
	if cfg.ReopenTimeout <= 0 {
		cfg.ReopenTimeout = 60 * time.Second
	}
	if cfg.MaxLineSize <= 0 {
		cfg.MaxLineSize = 64 * 1024
	}
	ctx, cancel := context.WithCancel(context.Background())

	t := &Tailer{
		cfg:    cfg,
		lines:  make(chan Line, 256),
		store:  store,
		ctx:    ctx,
		cancel: cancel,
		done:   make(chan struct{}),
	}
	return t
}

// Lines returns the read-only channel of log lines. Consumers should range over this.
func (t *Tailer) Lines() <-chan Line { return t.lines }

// Rotations returns the number of rotation events handled.
func (t *Tailer) Rotations() int64 { return t.rotations.Load() }

// BytesRead returns the total bytes emitted.
func (t *Tailer) BytesRead() int64 { return t.bytesRead.Load() }

// Errors returns the count of non-fatal errors.
func (t *Tailer) Errors() int64 { return t.errors.Load() }

// Run starts following the file. It blocks until ctx is cancelled or
// an unrecoverable error occurs. The Lines channel is closed when Run returns.
//
// Run is NOT safe to call multiple times on the same Tailer.
func (t *Tailer) Run(ctx context.Context) error {
	defer close(t.done)
	defer close(t.lines)
	defer t.closeFile() // ensure file handle is released on exit

	// Merge contexts: if either is cancelled, stop.
	mergedCtx, mergedCancel := context.WithCancel(ctx)
	defer mergedCancel()
	go func() {
		select {
		case <-t.ctx.Done():
			mergedCancel()
		case <-mergedCtx.Done():
			t.cancel()
		}
	}()

	// ── Phase 1: Initial open ───────────────────────────────────────────
	if err := t.initialOpen(); err != nil {
		return fmt.Errorf("tailer %s: initial open: %w", t.cfg.Path, err)
	}

	// ── Phase 2: Set up watchers ───────────────────────────────────────
	watcher, err := fsnotify.NewWatcher()
	if err != nil {
		return fmt.Errorf("tailer %s: fsnotify: %w", t.cfg.Path, err)
	}
	defer watcher.Close()

	parentDir := filepath.Dir(t.cfg.Path)
	if err := watcher.Add(parentDir); err != nil {
		return fmt.Errorf("tailer %s: watch dir %s: %w", t.cfg.Path, parentDir, err)
	}

	// Also watch the file itself for write events (low latency).
	t.addFileWatch(watcher)

	// ── Timers ──────────────────────────────────────────────────────────
	flushTicker := time.NewTicker(t.cfg.FlushInterval)
	defer flushTicker.Stop()

	pollTicker := time.NewTicker(t.cfg.PollInterval)
	defer pollTicker.Stop()

	// Drain timeout: when at EOF, we wait for events but also re-check
	// periodically to handle missed inotify events.
	drainBackoff := time.NewTicker(250 * time.Millisecond)
	defer drainBackoff.Stop()

	// ── Main loop ───────────────────────────────────────────────────────
	baseName := filepath.Base(t.cfg.Path)

	for {
		// Drain available data.
		atEOF := t.drain()

		if !atEOF {
			// drain returned because of a read error (not EOF).
			// Wait a bit and try to recover.
			t.errors.Add(1)
			select {
			case <-mergedCtx.Done():
				t.flushCheckpoint()
				return mergedCtx.Err()
			case <-time.After(t.cfg.PollInterval):
				t.recoverFile()
				continue
			}
		}

		// At EOF: wait for something to happen.
		select {
		case <-mergedCtx.Done():
			t.flushCheckpoint()
			return mergedCtx.Err()

		case <-flushTicker.C:
			t.flushCheckpoint()

		case <-pollTicker.C:
			// Periodic full stat check.
			t.pollStat()

			// After rotation, the file handle (and its watch) may have changed.
			// Re-register the file watch.
			t.addFileWatch(watcher)

		case event, ok := <-watcher.Events:
			if !ok {
				t.flushCheckpoint()
				return nil
			}
			action := t.classifyEvent(event, baseName)
			switch action {
			case actionReopen:
				t.recoverFile()
				t.rotations.Add(1)
				t.addFileWatch(watcher)
			case actionDrain:
				// New data written to current file — drain will pick it up.
			case actionRotate:
				// Rename detected — old fd still valid, drain will hit EOF.
				t.rotations.Add(1)
			}

		case <-drainBackoff.C:
			// No external event; just loop back to drain().
		}
	}
}

type eventAction int

const (
	actionNone   eventAction = iota
	actionDrain              // new data on current file
	actionReopen             // new file at path (create after rotation/removal)
	actionRotate             // current file renamed
)

func (t *Tailer) classifyEvent(event fsnotify.Event, baseName string) eventAction {
	// fsnotify events carry the full path in event.Name regardless of
	// whether the watch is on the file directly or on the parent directory.
	// Filter by the base name to only react to our file.
	if filepath.Base(event.Name) != baseName {
		return actionNone
	}

	switch {
	case event.Has(fsnotify.Write):
		// New data was written to the file we're actively watching.
		return actionDrain

	case event.Has(fsnotify.Create):
		// New file appeared at our path.
		// This is the "create" half of rename+create rotation,
		// or a file appearing after being removed.
		return actionReopen

	case event.Has(fsnotify.Rename), event.Has(fsnotify.Remove):
		// Our file was renamed or removed.
		// The old fd (if we have one) still points to valid data.
		// We'll drain it to EOF, then the next poll or create event
		// will re-open the path.
		return actionRotate
	}

	return actionNone
}

// addFileWatch adds/updates the fsnotify watch on the currently open file.
func (t *Tailer) addFileWatch(watcher *fsnotify.Watcher) {
	t.mu.Lock()
	defer t.mu.Unlock()

	if t.f == nil {
		return
	}

	// Remove any existing watches on the path first (idempotent).
	watcher.Remove(t.cfg.Path)
	// Add watch on the path (which follows symlinks on most platforms).
	watcher.Add(t.cfg.Path)
}

// ── Initial open ─────────────────────────────────────────────────────────────

func (t *Tailer) initialOpen() error {
	f, err := os.Open(t.cfg.Path)
	if err != nil {
		if os.IsNotExist(err) {
			// File doesn't exist yet — will be created later.
			return nil
		}
		return err
	}

	id, err := identity.Fstat(f)
	if err != nil {
		f.Close()
		return err
	}

	fi, err := f.Stat()
	if err != nil {
		f.Close()
		return err
	}
	fileSize := fi.Size()

	// Determine offset.
	offset := t.resolveOffset(id, fileSize)

	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		f.Close()
		return fmt.Errorf("seek to %d: %w", offset, err)
	}

	t.mu.Lock()
	t.f = f
	t.fileId = id
	t.offset = offset
	t.reader = bufio.NewReaderSize(f, t.cfg.MaxLineSize)
	t.lastFlush = time.Now()
	t.mu.Unlock()

	return nil
}

// resolveOffset determines the byte offset to start reading at.
// Priority: checkpoint > StartOffset config.
func (t *Tailer) resolveOffset(id identity.FileId, fileSize int64) int64 {
	if cp, ok := t.store.Get(t.cfg.Path); ok {
		if cp.IsValidFor(t.cfg.Path, id, fileSize) {
			return cp.Offset
		}
		// Checkpoint exists but is invalid (rotation, truncation).
		// Start from beginning.
		return 0
	}

	// No checkpoint — use config.
	if t.cfg.StartOffset < 0 {
		return fileSize // tail from end
	}
	if t.cfg.StartOffset > fileSize {
		return 0
	}
	return t.cfg.StartOffset
}

// ── Drain ────────────────────────────────────────────────────────────────────

// drain reads lines from the current file until EOF or error.
// Returns true if it stopped because of EOF (normal), false on error.
func (t *Tailer) drain() bool {
	t.mu.Lock()
	r := t.reader
	t.mu.Unlock()

	if r == nil {
		return true // no file open; treat as EOF
	}

	for {
		line, err := r.ReadString('\n')
		if err != nil {
			if errors.Is(err, io.EOF) {
				// Emit any partial trailing line.
				if len(line) > 0 {
					t.emit(line)
				}
				return true
			}
			// Read error (not EOF).
			return false
		}
		t.emit(line)
	}
}

// emit sends a line to the output channel and advances the offset.
func (t *Tailer) emit(text string) {
	n := int64(len(text))

	t.mu.Lock()
	off := t.offset
	t.offset += n
	t.mu.Unlock()

	select {
	case t.lines <- Line{Path: t.cfg.Path, Offset: off, Text: text}:
		t.bytesRead.Add(n)
	case <-t.ctx.Done():
	}
}

// ── Poll stat ────────────────────────────────────────────────────────────────

// pollStat checks the file for rotation, truncation, or reappearance.
func (t *Tailer) pollStat() {
	fi, err := os.Stat(t.cfg.Path)
	if err != nil {
		// File missing. If we have an fd, check whether it's still valid.
		t.mu.Lock()
		f := t.f
		t.mu.Unlock()
		if f != nil {
			if _, fErr := f.Stat(); fErr != nil {
				// Both the path and our fd are invalid.
				// Mark for reopen when file reappears.
				t.mu.Lock()
				t.closeLocked()
				t.mu.Unlock()
			}
			// else: path doesn't exist but our fd (renamed file) still does — keep draining.
		}
		return
	}

	newId := identity.FromFileInfo(fi)
	newSize := fi.Size()

	t.mu.Lock()
	currentId := t.fileId
	currentOffset := t.offset
	currentFile := t.f
	t.mu.Unlock()

	if currentFile == nil {
		// No file open — try to open.
		t.recoverFile()
		t.rotations.Add(1)
		return
	}

	if !newId.Equals(currentId) {
		// Inode changed: rotation via rename+create or symlink target change.
		t.handleRotation(currentId, newId, newSize)
		return
	}

	// Same inode: check for copytruncate.
	if newSize < currentOffset {
		t.handleTruncation(currentFile, newSize)
	}
}

// ── Rotation / truncation handlers ───────────────────────────────────────────

func (t *Tailer) handleRotation(oldId, newId identity.FileId, newSize int64) {
	// Phase 1: drain and close old fd under lock.
	t.mu.Lock()
	if t.f != nil {
		t.drainRemainingLocked()
		t.f.Close()
		t.f = nil
		t.reader = nil
	}
	t.mu.Unlock()

	// Phase 2: I/O outside the lock.
	f, err := os.Open(t.cfg.Path)
	if err != nil {
		return
	}

	// Phase 3: update state under lock.
	t.mu.Lock()
	defer t.mu.Unlock()

	t.f = f
	t.fileId = newId
	t.offset = 0
	t.reader = bufio.NewReaderSize(f, t.cfg.MaxLineSize)

	// Check checkpoint for the new identity.
	if cp, ok := t.store.Get(t.cfg.Path); ok {
		if cp.IsValidFor(t.cfg.Path, newId, newSize) {
			t.offset = cp.Offset
			f.Seek(t.offset, io.SeekStart)
		}
	}

	t.rotations.Add(1)
}

func (t *Tailer) handleTruncation(f *os.File, newSize int64) {
	t.mu.Lock()
	defer t.mu.Unlock()

	t.offset = 0
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		t.closeLocked()
		return
	}
	if t.reader != nil {
		t.reader.Reset(f)
	}
	t.rotations.Add(1)
}

// ── Recovery ─────────────────────────────────────────────────────────────────

// recoverFile opens or re-opens the configured path.
// I/O operations (open, stat, seek) are done outside the lock.
func (t *Tailer) recoverFile() {
	// Phase 1: close the old fd under lock.
	t.mu.Lock()
	if t.f != nil {
		t.drainRemainingLocked()
		t.f.Close()
		t.f = nil
		t.reader = nil
	}
	t.mu.Unlock()

	// Phase 2: I/O outside the lock.
	f, err := os.Open(t.cfg.Path)
	if err != nil {
		return
	}

	id, err := identity.Fstat(f)
	if err != nil {
		f.Close()
		return
	}

	fi, err := f.Stat()
	if err != nil {
		f.Close()
		return
	}
	fileSize := fi.Size()

	offset := int64(0)
	if cp, ok := t.store.Get(t.cfg.Path); ok {
		if cp.IsValidFor(t.cfg.Path, id, fileSize) {
			offset = cp.Offset
		}
	}
	if offset > fileSize {
		offset = 0
	}

	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		f.Close()
		return
	}

	// Phase 3: update state under lock.
	t.mu.Lock()
	t.f = f
	t.fileId = id
	t.offset = offset
	t.reader = bufio.NewReaderSize(f, t.cfg.MaxLineSize)
	t.mu.Unlock()
}

// drainRemainingLocked reads all remaining data from the current fd.
// Must hold t.mu.
func (t *Tailer) drainRemainingLocked() {
	if t.reader == nil {
		return
	}
	for {
		line, err := t.reader.ReadString('\n')
		if len(line) > 0 {
			n := int64(len(line))
			off := t.offset
			t.offset += n
			// Best-effort send; drop if channel is full (we're shutting down the fd).
			select {
			case t.lines <- Line{Path: t.cfg.Path, Offset: off, Text: line}:
				t.bytesRead.Add(n)
			default:
			}
		}
		if err != nil {
			break
		}
	}
}

// closeFile closes the current fd (thread-safe, for cleanup on shutdown).
func (t *Tailer) closeFile() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.f != nil {
		t.f.Close()
		t.f = nil
	}
	t.reader = nil
	t.fileId = identity.FileId{}
}

// closeLocked closes the current fd without draining. Must hold t.mu.
func (t *Tailer) closeLocked() {
	if t.f != nil {
		t.f.Close()
		t.f = nil
	}
	t.reader = nil
	t.fileId = identity.FileId{}
}

// ── Checkpoint ───────────────────────────────────────────────────────────────

func (t *Tailer) flushCheckpoint() {
	t.mu.Lock()
	path := t.cfg.Path
	id := t.fileId
	offset := t.offset
	t.mu.Unlock()

	if id.IsZero() {
		return
	}

	t.store.Update(path, id, offset)
	// Best-effort flush; errors are non-fatal for tailing.
	_ = t.store.Flush()
}

// FlushCheckpoint forces an immediate checkpoint flush.
func (t *Tailer) FlushCheckpoint() {
	t.flushCheckpoint()
}

// Shutdown gracefully stops the tailer, flushes the final checkpoint,
// and waits for Run to return.
func (t *Tailer) Shutdown(timeout time.Duration) error {
	t.cancel()
	t.flushCheckpoint()

	select {
	case <-t.done:
		return nil
	case <-time.After(timeout):
		return fmt.Errorf("tailer %s: shutdown timed out after %s", t.cfg.Path, timeout)
	}
}
