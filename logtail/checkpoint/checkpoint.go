// Package checkpoint provides atomic, durable persistence for file read offsets.
//
// Design goals:
//   - At-least-once semantics (we may re-process a small window after crash,
//     but we will never silently skip data).
//   - Identify files by (path, inode, device) — not path alone — so rotation is safe.
//   - Atomic writes via temp-file + fsync + rename.
//   - Batched/debounced writes, not write-on-every-line.
//   - On startup: if inode/dev matches and offset <= file size, seek to offset.
//     If offset > file size, treat as truncate and reset to 0.
//     If path matches but inode/dev changed, treat as new file.
package checkpoint

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/Yanghh0220/loggazer/logtail/identity"
)

// Entry is a single checkpoint record for one file.
type Entry struct {
	Path      string        `json:"path"`
	FileId    identity.FileId `json:"file_id"`
	Offset    int64         `json:"offset"`
	UpdatedAt time.Time     `json:"updated_at"`
}

// IsValidFor returns true if this entry can be used to resume reading the
// file at the given path with the given identity and current size.
//
// Rules:
//   - Path and identity must both match (rotation => new identity => start at 0).
//   - Offset <= currentSize: normal resume.
//   - Offset > currentSize: file was truncated, reset to 0.
func (e Entry) IsValidFor(path string, id identity.FileId, currentSize int64) bool {
	if e.Path != path {
		return false
	}
	if !e.FileId.Equals(id) {
		return false
	}
	// If the file shrank below our offset (copytruncate style), reset.
	if e.Offset > currentSize {
		return false
	}
	return true
}

// Store persists checkpoints to a JSON file on disk.
// All methods are safe for concurrent use.
type Store struct {
	mu       sync.Mutex
	dir      string
	filename string
	entries  map[string]Entry // keyed by path
}

// NewStore creates or opens a checkpoint store.
// dir is the directory where the checkpoint file will live.
func NewStore(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, fmt.Errorf("checkpoint.NewStore mkdir: %w", err)
	}
	s := &Store{
		dir:      dir,
		filename: filepath.Join(dir, "checkpoints.json"),
		entries:  make(map[string]Entry),
	}
	if err := s.load(); err != nil {
		if !os.IsNotExist(err) {
			return nil, fmt.Errorf("checkpoint.NewStore load: %w", err)
		}
		// File doesn't exist yet — create an empty one so callers
		// can stat the file immediately after NewStore.
		if err := s.flushLocked(); err != nil {
			return nil, fmt.Errorf("checkpoint.NewStore init flush: %w", err)
		}
	}
	return s, nil
}

// load reads the checkpoint file from disk (best-effort; corruption => start fresh).
func (s *Store) load() error {
	data, err := os.ReadFile(s.filename)
	if err != nil {
		return err
	}
	var entries []Entry
	if err := json.Unmarshal(data, &entries); err != nil {
		// Corrupted checkpoint file — log warning and start fresh
		return fmt.Errorf("checkpoint: corrupt checkpoint file, starting fresh: %w", err)
	}
	for _, e := range entries {
		s.entries[e.Path] = e
	}
	return nil
}

// Get returns the checkpoint for a path, and whether one existed.
func (s *Store) Get(path string) (Entry, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.entries[path]
	return e, ok
}

// Update records a new offset for a path+identity combination.
// This is an in-memory operation; call Flush() to persist.
func (s *Store) Update(path string, id identity.FileId, offset int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.entries[path] = Entry{
		Path:      path,
		FileId:    id,
		Offset:    offset,
		UpdatedAt: time.Now().UTC(),
	}
}

// Remove deletes the checkpoint for a path.
func (s *Store) Remove(path string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.entries, path)
}

// Flush atomically persists all entries to disk.
//
// Atomic protocol:
//   1. Write to a temporary file in the same directory.
//   2. Fsync the temp file to durable storage.
//   3. Rename the temp file over the target (atomic on POSIX).
func (s *Store) Flush() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.flushLocked()
}

func (s *Store) flushLocked() error {
	// Build ordered slice for deterministic output.
	entries := make([]Entry, 0, len(s.entries))
	for _, e := range s.entries {
		entries = append(entries, e)
	}

	data, err := json.MarshalIndent(entries, "", "  ")
	if err != nil {
		return fmt.Errorf("checkpoint.Flush marshal: %w", err)
	}

	tmpPath := s.filename + ".tmp"

	// Open temp file with write access: Windows requires GENERIC_WRITE for Sync.
	f, err := os.OpenFile(tmpPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0600)
	if err != nil {
		return fmt.Errorf("checkpoint.Flush create tmp: %w", err)
	}

	if _, err := f.Write(data); err != nil {
		f.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("checkpoint.Flush write tmp: %w", err)
	}

	if err := f.Sync(); err != nil {
		f.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("checkpoint.Flush fsync: %w", err)
	}
	f.Close()

	// Atomic rename
	if err := os.Rename(tmpPath, s.filename); err != nil {
		os.Remove(tmpPath) // best-effort cleanup
		return fmt.Errorf("checkpoint.Flush rename: %w", err)
	}

	return nil
}

// Close flushes and releases resources.
func (s *Store) Close() error {
	return s.Flush()
}
