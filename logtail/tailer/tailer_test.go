package tailer

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/Yanghh0220/loggazer/logtail/checkpoint"
)

// waitLines collects lines from a tailer with a timeout.
func waitLines(t *testing.T, tailer *Tailer, expected int, timeout time.Duration) []Line {
	t.Helper()
	var lines []Line
	deadline := time.After(timeout)
	for len(lines) < expected {
		select {
		case line, ok := <-tailer.Lines():
			if !ok {
				return lines
			}
			lines = append(lines, line)
		case <-deadline:
			return lines
		}
	}
	// Drain any extra lines.
	for {
		select {
		case line, ok := <-tailer.Lines():
			if !ok {
				return lines
			}
			lines = append(lines, line)
		case <-time.After(100 * time.Millisecond):
			return lines
		}
	}
}

// writeAndSync writes data to a file and ensures it's flushed.
// Close already flushes on all platforms; explicit Sync is omitted to avoid
// Windows file-locking conflicts with concurrent readers.
func writeAndSync(t *testing.T, path string, data string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(data); err != nil {
		f.Close()
		t.Fatal(err)
	}
	f.Close()
}

// ── Basic tailing ────────────────────────────────────────────────────────────

func TestTailerBasicRead(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "test.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	writeAndSync(t, logPath, "line1\nline2\nline3\n")

	store, err := checkpoint.NewStore(ckptDir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	tailer := New(Config{
		Path:          logPath,
		PollInterval:  100 * time.Millisecond,
		FlushInterval: 10 * time.Second,
		StartOffset:   0,
	}, store)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go tailer.Run(ctx)

	lines := waitLines(t, tailer, 3, 2*time.Second)
	if len(lines) < 3 {
		t.Fatalf("expected at least 3 lines, got %d", len(lines))
	}

	if lines[0].Text != "line1\n" {
		t.Errorf("line0 = %q, want %q", lines[0].Text, "line1\n")
	}
	if lines[1].Text != "line2\n" {
		t.Errorf("line1 = %q, want %q", lines[1].Text, "line2\n")
	}
	if lines[2].Text != "line3\n" {
		t.Errorf("line2 = %q, want %q", lines[2].Text, "line3\n")
	}

	cancel()
	tailer.Shutdown(2 * time.Second)
}

func TestTailerStartAtEnd(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "test.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	writeAndSync(t, logPath, "old1\nold2\n")

	store, err := checkpoint.NewStore(ckptDir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	tailer := New(Config{
		Path:          logPath,
		PollInterval:  100 * time.Millisecond,
		FlushInterval: 10 * time.Second,
		StartOffset:   -1,
	}, store)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go tailer.Run(ctx)

	time.Sleep(300 * time.Millisecond)
	writeAndSync(t, logPath, "new1\nnew2\n")

	lines := waitLines(t, tailer, 2, 2*time.Second)
	if len(lines) < 2 {
		t.Fatalf("expected 2 lines, got %d", len(lines))
	}
	if lines[0].Text != "new1\n" {
		t.Errorf("line0 = %q, want new1\\n", lines[0].Text)
	}

	cancel()
	tailer.Shutdown(2 * time.Second)
}

// ── Rotation: rename + create ────────────────────────────────────────────────

func TestTailerRenameCreateRotation(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("rename on open files is not supported on Windows")
	}

	dir := t.TempDir()
	logPath := filepath.Join(dir, "app.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	writeAndSync(t, logPath, "pre-rotate-1\npre-rotate-2\n")

	store, err := checkpoint.NewStore(ckptDir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	tailer := New(Config{
		Path:          logPath,
		PollInterval:  200 * time.Millisecond,
		FlushInterval: 10 * time.Second,
		StartOffset:   0,
	}, store)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go tailer.Run(ctx)

	lines := waitLines(t, tailer, 2, 2*time.Second)
	if len(lines) < 2 {
		t.Fatalf("expected 2 pre-rotation lines, got %d", len(lines))
	}

	rotatedPath := logPath + ".1"
	if err := os.Rename(logPath, rotatedPath); err != nil {
		t.Fatal(err)
	}

	writeAndSync(t, rotatedPath, "dangling-line\n")
	writeAndSync(t, logPath, "post-rotate-1\npost-rotate-2\n")

	allLines := waitLines(t, tailer, 5, 5*time.Second)

	foundDangling := false
	foundPost1 := false
	foundPost2 := false
	for _, l := range allLines {
		switch strings.TrimSpace(l.Text) {
		case "dangling-line":
			foundDangling = true
		case "post-rotate-1":
			foundPost1 = true
		case "post-rotate-2":
			foundPost2 = true
		}
	}

	if !foundDangling {
		t.Error("missing dangling-line from old fd after rotation")
	}
	if !foundPost1 {
		t.Error("missing post-rotate-1 from new file")
	}
	if !foundPost2 {
		t.Error("missing post-rotate-2 from new file")
	}

	if tailer.Rotations() == 0 {
		t.Error("expected at least 1 rotation to be detected")
	}

	t.Logf("rotations detected: %d, bytes read: %d", tailer.Rotations(), tailer.BytesRead())

	cancel()
	tailer.Shutdown(2 * time.Second)
}

// ── Rotation: copytruncate ───────────────────────────────────────────────────

func TestTailerCopyTruncate(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "app.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	writeAndSync(t, logPath, "before-truncate-1\nbefore-truncate-2\n")

	store, err := checkpoint.NewStore(ckptDir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	tailer := New(Config{
		Path:          logPath,
		PollInterval:  200 * time.Millisecond,
		FlushInterval: 10 * time.Second,
		StartOffset:   0,
	}, store)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go tailer.Run(ctx)

	lines := waitLines(t, tailer, 2, 2*time.Second)
	if len(lines) < 2 {
		t.Fatalf("expected 2 pre-trunc lines, got %d", len(lines))
	}

	if err := os.Truncate(logPath, 0); err != nil {
		t.Fatal(err)
	}
	time.Sleep(500 * time.Millisecond)
	writeAndSync(t, logPath, "after-truncate-1\nafter-truncate-2\n")

	allLines := waitLines(t, tailer, 4, 5*time.Second)

	foundAfter := false
	for _, l := range allLines {
		if strings.TrimSpace(l.Text) == "after-truncate-1" {
			foundAfter = true
		}
	}
	if !foundAfter {
		t.Error("missing after-truncate-1 (copytruncate handling failed)")
		t.Logf("received lines: %v", allLines)
	}

	if tailer.Rotations() == 0 {
		t.Error("expected copytruncate to be detected as rotation")
	}

	cancel()
	tailer.Shutdown(2 * time.Second)
}

// ── Checkpoint resume ────────────────────────────────────────────────────────

func TestTailerCheckpointResume(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "app.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	// Write first 3 lines; tailer reads them, then we checkpoint and stop.
	writeAndSync(t, logPath, "line1\nline2\nline3\n")

	store, err := checkpoint.NewStore(ckptDir)
	if err != nil {
		t.Fatal(err)
	}

	tailer1 := New(Config{
		Path:          logPath,
		PollInterval:  100 * time.Millisecond,
		FlushInterval: 10 * time.Second,
		StartOffset:   0,
	}, store)

	ctx1, cancel1 := context.WithCancel(context.Background())
	go tailer1.Run(ctx1)

	lines1 := waitLines(t, tailer1, 3, 2*time.Second)
	if len(lines1) < 3 {
		t.Fatalf("first session: expected 3 lines, got %d", len(lines1))
	}

	tailer1.FlushCheckpoint()

	cancel1()
	if err := tailer1.Shutdown(2 * time.Second); err != nil {
		t.Fatalf("shutdown tailer1: %v", err)
	}
	store.Close()

	// Write remaining lines for second session.
	writeAndSync(t, logPath, "line4\nline5\n")

	store2, err := checkpoint.NewStore(ckptDir)
	if err != nil {
		t.Fatal(err)
	}
	defer store2.Close()

	cp, ok := store2.Get(logPath)
	if !ok {
		t.Fatal("expected checkpoint to exist after first session")
	}
	t.Logf("checkpoint offset: %d", cp.Offset)
	if cp.Offset == 0 {
		t.Error("expected non-zero checkpoint offset")
	}

	tailer2 := New(Config{
		Path:          logPath,
		PollInterval:  100 * time.Millisecond,
		FlushInterval: 10 * time.Second,
		StartOffset:   0,
	}, store2)

	ctx2, cancel2 := context.WithCancel(context.Background())
	defer cancel2()

	go tailer2.Run(ctx2)

	lines2 := waitLines(t, tailer2, 2, 2*time.Second)
	if len(lines2) < 2 {
		t.Fatalf("second session: expected 2 lines, got %d", len(lines2))
	}

	t.Logf("resumed lines: %v", lines2)

	allTexts := make(map[string]bool)
	for _, l := range lines1 {
		allTexts[strings.TrimSpace(l.Text)] = true
	}
	for _, l := range lines2 {
		allTexts[strings.TrimSpace(l.Text)] = true
	}
	if len(allTexts) != 5 {
		t.Errorf("expected 5 unique lines total, got %d", len(allTexts))
	}

	cancel2()
	tailer2.Shutdown(2 * time.Second)
}

// ── Rotation with checkpoint ─────────────────────────────────────────────────

func TestTailerRotationCheckpointSafety(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("rename on open files is not supported on Windows")
	}

	dir := t.TempDir()
	logPath := filepath.Join(dir, "app.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	writeAndSync(t, logPath, "gen1-line1\ngen1-line2\n")

	store, err := checkpoint.NewStore(ckptDir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	tailer := New(Config{
		Path:          logPath,
		PollInterval:  200 * time.Millisecond,
		FlushInterval: 10 * time.Second,
		StartOffset:   0,
	}, store)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go tailer.Run(ctx)

	waitLines(t, tailer, 2, 2*time.Second)

	rotatedPath := logPath + ".1"
	if err := os.Rename(logPath, rotatedPath); err != nil {
		t.Fatal(err)
	}
	writeAndSync(t, rotatedPath, "gen1-dangling\n")
	writeAndSync(t, logPath, "gen2-line1\ngen2-line2\n")

	lines := waitLines(t, tailer, 3, 5*time.Second)

	hasDangling := false
	hasGen2 := false
	for _, l := range lines {
		switch strings.TrimSpace(l.Text) {
		case "gen1-dangling":
			hasDangling = true
		case "gen2-line1", "gen2-line2":
			hasGen2 = true
		}
	}
	if !hasDangling {
		t.Error("missing dangling line from old generation")
	}
	if !hasGen2 {
		t.Error("missing lines from new generation")
	}

	cancel()
	tailer.Shutdown(2 * time.Second)
}

// ── Truncation with offset > file size ───────────────────────────────────────

func TestTailerTruncationResetsOffset(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "app.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	var sb strings.Builder
	for i := 0; i < 100; i++ {
		sb.WriteString("line content that takes space\n")
	}
	writeAndSync(t, logPath, sb.String())

	store, err := checkpoint.NewStore(ckptDir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	tailer := New(Config{
		Path:          logPath,
		PollInterval:  300 * time.Millisecond,
		FlushInterval: 10 * time.Second,
		StartOffset:   0,
	}, store)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go tailer.Run(ctx)

	waitLines(t, tailer, 10, 2*time.Second)

	tailer.FlushCheckpoint()

	if err := os.Truncate(logPath, 0); err != nil {
		t.Fatal(err)
	}
	time.Sleep(500 * time.Millisecond)
	writeAndSync(t, logPath, "after-truncate-1\nafter-truncate-2\n")

	lines := waitLines(t, tailer, 2, 3*time.Second)

	hasNew := false
	for _, l := range lines {
		if strings.Contains(l.Text, "after-truncate") {
			hasNew = true
		}
	}
	if !hasNew {
		t.Error("did not receive new lines after truncation")
	}

	cancel()
	tailer.Shutdown(2 * time.Second)
}

// ── Non-existent file initially ──────────────────────────────────────────────

func TestTailerFileAppearsLater(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "late.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	store, err := checkpoint.NewStore(ckptDir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	tailer := New(Config{
		Path:          logPath,
		PollInterval:  100 * time.Millisecond,
		FlushInterval: 10 * time.Second,
		StartOffset:   0,
	}, store)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go tailer.Run(ctx)

	time.Sleep(300 * time.Millisecond)

	writeAndSync(t, logPath, "hello\nworld\n")

	lines := waitLines(t, tailer, 2, 2*time.Second)
	if len(lines) < 2 {
		t.Fatalf("expected 2 lines, got %d", len(lines))
	}

	cancel()
	tailer.Shutdown(2 * time.Second)
}

// ── Shutdown flushes checkpoint ──────────────────────────────────────────────

func TestTailerShutdownFlushesCheckpoint(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "app.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	writeAndSync(t, logPath, "line1\nline2\nline3\n")

	store, err := checkpoint.NewStore(ckptDir)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	tailer := New(Config{
		Path:          logPath,
		PollInterval:  100 * time.Millisecond,
		FlushInterval: 1 * time.Hour,
		StartOffset:   0,
	}, store)

	ctx, cancel := context.WithCancel(context.Background())
	go tailer.Run(ctx)

	waitLines(t, tailer, 3, 2*time.Second)

	cancel()
	if err := tailer.Shutdown(2 * time.Second); err != nil {
		t.Fatal(err)
	}

	cp, ok := store.Get(logPath)
	if !ok {
		t.Fatal("checkpoint should exist after shutdown")
	}
	if cp.Offset == 0 {
		t.Error("expected non-zero offset in checkpoint after shutdown")
	}
	t.Logf("final checkpoint offset: %d", cp.Offset)
}
