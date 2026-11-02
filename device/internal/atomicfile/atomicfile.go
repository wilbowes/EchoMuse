// Package atomicfile replaces a file so that a power cut leaves either the
// old contents or the new, never an empty file.
package atomicfile

import (
	"os"
	"path/filepath"
)

// Write puts data at path through a temp file beside it: the temp file is
// synced before the rename and the directory after it. Without the first
// sync ext4 can commit the rename ahead of the data, and a power cut seconds
// after a mute left a zero-length state.json (VVV, 2026-10-11, #849). A
// missing directory is made: the saved echo path and Bluetooth keys relied on
// start_server.sh having made it, and a firmware started any other way could
// not save them. MkdirAll on an existing directory only stats it.
//
// An error from the directory sync is returned although the file is already
// in place: the rename may not survive a power cut.
func Write(path string, data []byte, perm os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := writeSynced(tmp, data, perm); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return syncDir(filepath.Dir(path))
}

// writeSynced is os.WriteFile that returns only once the data is on flash.
func writeSynced(path string, data []byte, perm os.FileMode) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, perm)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// syncDir makes a rename in dir survive a power cut.
func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
