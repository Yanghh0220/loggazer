package manager

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/Yanghh0220/loggazer/logtail/tailer"
)

func writeFile(t *testing.T, path string, data string) {
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

func TestManagerSingleFile(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "test.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	writeFile(t, logPath, "line1\nline2\nline3\n")

	mgr, err := New(Config{
		CheckpointDir:        ckptDir,
		Files:                []tailer.Config{{Path: logPath, StartOffset: 0}},
		GlobalPollInterval:   100 * time.Millisecond,
		GlobalFlushInterval:  10 * time.Second,
		ShutdownTimeout:      5 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	done := make(chan struct{})
	go func() {
		mgr.Run(ctx)
		close(done)
	}()

	var lines []tailer.Line
	deadline := time.After(2 * time.Second)
loop:
	for {
		select {
		case line, ok := <-mgr.Lines():
			if !ok {
				break loop
			}
			lines = append(lines, line)
			if len(lines) >= 3 {
				break loop
			}
		case <-deadline:
			break loop
		}
	}

	if len(lines) < 3 {
		t.Fatalf("expected at least 3 lines, got %d", len(lines))
	}
	for i, l := range lines[:3] {
		if l.Path != logPath {
			t.Errorf("line %d: expected path %s, got %s", i, logPath, l.Path)
		}
	}

	cancel()
	<-done
}

func TestManagerMultipleFiles(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("file cleanup order issues on Windows with multiple tailers")
	}

	dir := t.TempDir()
	ckptDir := filepath.Join(dir, "checkpoints")

	path1 := filepath.Join(dir, "a.log")
	path2 := filepath.Join(dir, "b.log")

	writeFile(t, path1, "a1\na2\n")
	writeFile(t, path2, "b1\nb2\n")

	mgr, err := New(Config{
		CheckpointDir: ckptDir,
		Files: []tailer.Config{
			{Path: path1, StartOffset: 0},
			{Path: path2, StartOffset: 0},
		},
		GlobalPollInterval:  100 * time.Millisecond,
		GlobalFlushInterval: 10 * time.Second,
		ShutdownTimeout:     5 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	done := make(chan struct{})
	go func() {
		mgr.Run(ctx)
		close(done)
	}()

	var lines []tailer.Line
	deadline := time.After(2 * time.Second)
loop:
	for {
		select {
		case line, ok := <-mgr.Lines():
			if !ok {
				break loop
			}
			lines = append(lines, line)
			if len(lines) >= 4 {
				break loop
			}
		case <-deadline:
			break loop
		}
	}

	if len(lines) < 4 {
		t.Fatalf("expected at least 4 lines from 2 files, got %d", len(lines))
	}

	gotA := false
	gotB := false
	for _, l := range lines {
		if l.Path == path1 {
			gotA = true
		}
		if l.Path == path2 {
			gotB = true
		}
	}
	if !gotA || !gotB {
		t.Errorf("expected lines from both files: gotA=%v gotB=%v", gotA, gotB)
	}

	cancel()
	<-done
}

func TestManagerStats(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "test.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	writeFile(t, logPath, strings.Repeat("data\n", 50))

	mgr, err := New(Config{
		CheckpointDir:        ckptDir,
		Files:                []tailer.Config{{Path: logPath, StartOffset: 0}},
		GlobalPollInterval:   100 * time.Millisecond,
		GlobalFlushInterval:  10 * time.Second,
		ShutdownTimeout:      5 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	done := make(chan struct{})
	go func() {
		mgr.Run(ctx)
		close(done)
	}()

	deadline := time.After(2 * time.Second)
	var lineCount int
loop:
	for {
		select {
		case _, ok := <-mgr.Lines():
			if !ok {
				break loop
			}
			lineCount++
			if lineCount >= 10 {
				break loop
			}
		case <-deadline:
			break loop
		}
	}

	stats := mgr.Stats()
	if stats.Files != 1 {
		t.Errorf("expected 1 file, got %d", stats.Files)
	}
	if stats.BytesRead == 0 {
		t.Error("expected non-zero BytesRead")
	}
	t.Logf("stats: %+v, lines collected: %d", stats, lineCount)

	cancel()
	<-done
}

func TestManagerGracefulShutdown(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "test.log")
	ckptDir := filepath.Join(dir, "checkpoints")

	writeFile(t, logPath, "line1\nline2\n")

	mgr, err := New(Config{
		CheckpointDir:        ckptDir,
		Files:                []tailer.Config{{Path: logPath, StartOffset: 0}},
		GlobalPollInterval:   100 * time.Millisecond,
		GlobalFlushInterval:  100 * time.Millisecond,
		ShutdownTimeout:      5 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan error, 1)
	go func() {
		done <- mgr.Run(ctx)
	}()

	deadline := time.After(2 * time.Second)
	var count int
loop:
	for {
		select {
		case _, ok := <-mgr.Lines():
			if !ok {
				break loop
			}
			count++
			if count >= 2 {
				break loop
			}
		case <-deadline:
			break loop
		}
	}

	cancel()

	select {
	case err := <-done:
		if err != nil && err != context.Canceled {
			t.Errorf("unexpected error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("shutdown timed out")
	}

	if _, err := os.Stat(filepath.Join(ckptDir, "checkpoints.json")); err != nil {
		t.Errorf("checkpoint file not found after shutdown: %v", err)
	}
}
