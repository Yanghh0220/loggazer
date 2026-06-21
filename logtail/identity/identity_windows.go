//go:build windows

package identity

import (
	"os"
)

// FromFileInfo extracts the platform identity from os.FileInfo.
// On Windows, inodes are not stable across renames, so we use
// size + modtime as a weak identity.
func FromFileInfo(fi os.FileInfo) FileId {
	return FileId{
		Dev: uint64(fi.ModTime().UnixNano()),
		Ino: uint64(fi.Size()),
	}
}
