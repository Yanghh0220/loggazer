package checkpoint

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/Yanghh0220/loggazer/logtail/identity"
)

func TestNewStore(t *testing.T) {
	dir := t.TempDir()
	s, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	if s == nil {
		t.Fatal("expected non-nil store")
	}

	// Should have created the checkpoint file (even if empty).
	_, err = os.Stat(s.filename)
	if err != nil {
		t.Fatalf("expected checkpoint file to exist: %v", err)
	}
}

func TestUpdateAndGet(t *testing.T) {
	dir := t.TempDir()
	s, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	id := identity.FileId{Dev: 0x801, Ino: 42}
	s.Update("/var/log/app.log", id, 1024)

	entry, ok := s.Get("/var/log/app.log")
	if !ok {
		t.Fatal("expected entry to exist")
	}
	if entry.Path != "/var/log/app.log" {
		t.Errorf("expected path /var/log/app.log, got %s", entry.Path)
	}
	if !entry.FileId.Equals(id) {
		t.Errorf("expected file id %v, got %v", id, entry.FileId)
	}
	if entry.Offset != 1024 {
		t.Errorf("expected offset 1024, got %d", entry.Offset)
	}
	if entry.UpdatedAt.IsZero() {
		t.Error("expected non-zero UpdatedAt")
	}
}

func TestFlushAndReload(t *testing.T) {
	dir := t.TempDir()
	s, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}

	id1 := identity.FileId{Dev: 0x801, Ino: 100}
	s.Update("/var/log/a.log", id1, 500)
	s.Update("/var/log/b.log", identity.FileId{Dev: 0x802, Ino: 200}, 1500)

	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}
	s.Close()

	// Reload from disk.
	s2, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()

	entry, ok := s2.Get("/var/log/a.log")
	if !ok {
		t.Fatal("expected entry for a.log after reload")
	}
	if entry.Offset != 500 {
		t.Errorf("expected offset 500, got %d", entry.Offset)
	}
	if !entry.FileId.Equals(id1) {
		t.Errorf("file id mismatch after reload")
	}

	entry2, ok := s2.Get("/var/log/b.log")
	if !ok {
		t.Fatal("expected entry for b.log after reload")
	}
	if entry2.Offset != 1500 {
		t.Errorf("expected offset 1500, got %d", entry2.Offset)
	}
}

func TestAtomicFlush(t *testing.T) {
	dir := t.TempDir()
	s, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}

	s.Update("/tmp/test.log", identity.FileId{Dev: 1, Ino: 1}, 9999)

	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}

	// Verify no .tmp file lingers.
	tmpPath := s.filename + ".tmp"
	if _, err := os.Stat(tmpPath); !os.IsNotExist(err) {
		t.Error("expected .tmp file to be renamed (not exist)")
	}

	// Verify the real file exists and has content.
	data, err := os.ReadFile(s.filename)
	if err != nil {
		t.Fatal(err)
	}
	if len(data) == 0 {
		t.Error("expected non-empty checkpoint file")
	}
	s.Close()
}

func TestIsValidFor(t *testing.T) {
	id1 := identity.FileId{Dev: 0x801, Ino: 100}
	now := time.Now().UTC()

	tests := []struct {
		name    string
		entry   Entry
		path    string
		id      identity.FileId
		size    int64
		isValid bool
	}{
		{
			name:    "exact match",
			entry:   Entry{Path: "/a.log", FileId: id1, Offset: 100, UpdatedAt: now},
			path:    "/a.log",
			id:      id1,
			size:    500,
			isValid: true,
		},
		{
			name:    "offset at exact EOF",
			entry:   Entry{Path: "/a.log", FileId: id1, Offset: 500, UpdatedAt: now},
			path:    "/a.log",
			id:      id1,
			size:    500,
			isValid: true,
		},
		{
			name:    "offset beyond file size (truncation)",
			entry:   Entry{Path: "/a.log", FileId: id1, Offset: 1000, UpdatedAt: now},
			path:    "/a.log",
			id:      id1,
			size:    500,
			isValid: false,
		},
		{
			name:    "different inode (rotation)",
			entry:   Entry{Path: "/a.log", FileId: id1, Offset: 100, UpdatedAt: now},
			path:    "/a.log",
			id:      identity.FileId{Dev: 0x801, Ino: 999},
			size:    500,
			isValid: false,
		},
		{
			name:    "different path",
			entry:   Entry{Path: "/a.log", FileId: id1, Offset: 100, UpdatedAt: now},
			path:    "/b.log",
			id:      id1,
			size:    500,
			isValid: false,
		},
		{
			name:    "zero offset on empty file",
			entry:   Entry{Path: "/a.log", FileId: id1, Offset: 0, UpdatedAt: now},
			path:    "/a.log",
			id:      id1,
			size:    0,
			isValid: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := tt.entry.IsValidFor(tt.path, tt.id, tt.size)
			if got != tt.isValid {
				t.Errorf("IsValidFor = %v, want %v", got, tt.isValid)
			}
		})
	}
}

func TestStoreConcurrent(t *testing.T) {
	dir := t.TempDir()
	s, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	done := make(chan struct{})
	go func() {
		for i := 0; i < 100; i++ {
			s.Update("/tmp/log.log", identity.FileId{Dev: 1, Ino: uint64(i)}, int64(i*100))
		}
		close(done)
	}()

	// Concurrent reads.
	for i := 0; i < 50; i++ {
		s.Get("/tmp/log.log")
		s.Flush()
	}
	<-done

	// Final entry should be from the goroutine (last i=99).
	entry, ok := s.Get("/tmp/log.log")
	if !ok {
		t.Fatal("expected entry to exist")
	}
	if entry.Offset < 0 {
		t.Errorf("unexpected offset: %d", entry.Offset)
	}
}

func TestRemove(t *testing.T) {
	dir := t.TempDir()
	s, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	s.Update("/tmp/remove.log", identity.FileId{Dev: 1, Ino: 1}, 100)
	_, ok := s.Get("/tmp/remove.log")
	if !ok {
		t.Fatal("expected entry to exist before remove")
	}

	s.Remove("/tmp/remove.log")
	_, ok = s.Get("/tmp/remove.log")
	if ok {
		t.Fatal("expected entry to be removed")
	}
}

func TestNewStoreCreatesDir(t *testing.T) {
	base := t.TempDir()
	nestedDir := filepath.Join(base, "deep", "nested", "checkpoints")
	s, err := NewStore(nestedDir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	fi, err := os.Stat(nestedDir)
	if err != nil {
		t.Fatal(err)
	}
	if !fi.IsDir() {
		t.Error("expected nested dir to be created")
	}
}
