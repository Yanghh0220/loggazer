//go:build !windows

package identity

import (
	"os"
	"syscall"
)

// FromFileInfo extracts the platform identity from os.FileInfo.
// On Unix/Linux, uses (device_id, inode) from syscall.Stat_t.
func FromFileInfo(fi os.FileInfo) FileId {
	if sys := fi.Sys(); sys != nil {
		if st, ok := sys.(*syscall.Stat_t); ok {
			return FileId{Dev: st.Dev, Ino: st.Ino}
		}
	}
	// Fallback: use size + modtime as a weak identity.
	return FileId{
		Dev: uint64(fi.ModTime().UnixNano()),
		Ino: uint64(fi.Size()),
	}
}
