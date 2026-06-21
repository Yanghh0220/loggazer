package identity

import (
	"os"
	"path/filepath"
	"testing"
)

func TestStat(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.txt")
	if err := os.WriteFile(path, []byte("hello"), 0644); err != nil {
		t.Fatal(err)
	}

	id, err := Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if id.IsZero() {
		t.Error("expected non-zero identity for existing file")
	}
}

func TestStatNonExistent(t *testing.T) {
	_, err := Stat("/nonexistent/path/12345")
	if err == nil {
		t.Error("expected error for non-existent file")
	}
}

func TestFstat(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.txt")
	if err := os.WriteFile(path, []byte("world"), 0644); err != nil {
		t.Fatal(err)
	}

	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()

	id, err := Fstat(f)
	if err != nil {
		t.Fatal(err)
	}
	if id.IsZero() {
		t.Error("expected non-zero identity from Fstat")
	}
}

func TestEquals(t *testing.T) {
	id1 := FileId{Dev: 0x801, Ino: 42}
	id2 := FileId{Dev: 0x801, Ino: 42}
	id3 := FileId{Dev: 0x801, Ino: 43}
	id4 := FileId{Dev: 0x802, Ino: 42}

	if !id1.Equals(id2) {
		t.Error("expected id1 == id2")
	}
	if id1.Equals(id3) {
		t.Error("expected id1 != id3 (different inode)")
	}
	if id1.Equals(id4) {
		t.Error("expected id1 != id4 (different device)")
	}
}

func TestIsZero(t *testing.T) {
	var id FileId
	if !id.IsZero() {
		t.Error("expected zero value to be IsZero")
	}

	id2 := FileId{Dev: 1, Ino: 0}
	if id2.IsZero() {
		t.Error("expected non-zero dev to be !IsZero")
	}
}

func TestString(t *testing.T) {
	id := FileId{Dev: 0x801, Ino: 42}
	s := id.String()
	if s == "" {
		t.Error("expected non-empty string")
	}
	if s != "801:2a" {
		t.Errorf("expected '801:2a', got %q", s)
	}
}

func TestSameFileSameIdentity(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.txt")
	if err := os.WriteFile(path, []byte("same"), 0644); err != nil {
		t.Fatal(err)
	}

	id1, _ := Stat(path)
	id2, _ := Stat(path)

	if !id1.Equals(id2) {
		t.Error("same file should have same identity")
	}
}

func TestDifferentFilesDifferentIdentity(t *testing.T) {
	dir := t.TempDir()
	path1 := filepath.Join(dir, "a.txt")
	path2 := filepath.Join(dir, "b.txt")
	// Use different sizes so the weak identity (size+modtime) works
	// even on Windows where inodes are not available.
	os.WriteFile(path1, []byte("hello"), 0644)
	os.WriteFile(path2, []byte("world!"), 0644)

	id1, _ := Stat(path1)
	id2, _ := Stat(path2)

	if id1.Equals(id2) {
		t.Error("different files should have different identities")
	}
}
