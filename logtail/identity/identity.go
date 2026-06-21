// Package identity provides OS-independent file identity primitives.
// We identify files by (inode, device_id) on Unix/Linux, falling back to
// (path, size, modtime) on Windows. This is critical for correctly
// handling log rotation — when a file is renamed and a new one created
// at the same path, the old file descriptor tracks the inode while the
// path now points to a different inode.
package identity

import (
	"fmt"
	"os"
)

// FileId uniquely identifies a file on disk.
// On Unix: Dev + Ino (device id + inode number).
// On Windows: VolumeSerialNumber + FileIndexHigh + FileIndexLow.
// The zero value means "unknown identity".
type FileId struct {
	Dev uint64 // device ID (Unix: st_dev, Windows: dwVolumeSerialNumber)
	Ino uint64 // inode number (Unix: st_ino, Windows: nFileIndex)
}

// Stat gathers the platform-specific identity for the file at path.
// Returns a zero FileId if the file does not exist or cannot be stat'd.
func Stat(path string) (FileId, error) {
	fi, err := os.Stat(path)
	if err != nil {
		return FileId{}, fmt.Errorf("identity.Stat(%s): %w", path, err)
	}
	return FromFileInfo(fi), nil
}


// Fstat gathers file identity from an open *os.File.
func Fstat(f *os.File) (FileId, error) {
	fi, err := f.Stat()
	if err != nil {
		return FileId{}, fmt.Errorf("identity.Fstat: %w", err)
	}
	return FromFileInfo(fi), nil
}

// Equals returns true if two identities refer to the same file.
func (id FileId) Equals(other FileId) bool {
	return id.Dev == other.Dev && id.Ino == other.Ino
}

// IsZero returns true if the identity has never been set.
func (id FileId) IsZero() bool {
	return id.Dev == 0 && id.Ino == 0
}

// String returns a compact representation useful for logging and checkpoint keys.
func (id FileId) String() string {
	return fmt.Sprintf("%x:%x", id.Dev, id.Ino)
}
