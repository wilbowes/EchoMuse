package server

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
)

// statePath persists mute state across reboots and OTA restarts. It lives
// next to the TLS credentials in /data/local/etc — OTA slot flips only touch
// /data/local/bin, so the file survives them.
//
// Only mute lives here: mute is device-sovereign (the physical button cannot
// be overridden remotely, so the device must restore it itself, controller or
// no controller). Volume is the opposite — the controller's stored
// startupVolume is the source of truth, re-applied via SeedVolume on the
// first config push each run.
const statePath = "/data/local/etc/echomuse/state.json"

type deviceState struct {
	Muted bool `json:"muted"`
}

// loadDeviceState reads the persisted state. ok=false means no usable state
// (first boot of persisting firmware, or unreadable file) — boot unmuted as
// before.
func loadDeviceState(path string) (st deviceState, ok bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return deviceState{}, false
	}
	if err := json.Unmarshal(data, &st); err != nil {
		log.Printf("[state] %s unreadable, ignoring: %v", path, err)
		return deviceState{}, false
	}
	return st, true
}

// saveDeviceState writes the state atomically and durably: the temp file is
// synced before the rename and the directory after it. Without the first
// sync ext4 can commit the rename ahead of the data, and a power cut seconds
// after a mute left a zero-length state.json, so the Echo booted unmuted
// (VVV, 2026-10-11). Called on every mute toggle — rare enough that a
// synchronous flash write needs no debounce.
func saveDeviceState(path string, st deviceState) {
	data, err := json.Marshal(st)
	if err != nil {
		log.Printf("[state] marshal failed: %v", err)
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		log.Printf("[state] mkdir failed: %v", err)
		return
	}
	tmp := path + ".tmp"
	if err := writeFileSync(tmp, data, 0o644); err != nil {
		log.Printf("[state] write failed: %v", err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		log.Printf("[state] rename failed: %v", err)
		return
	}
	if err := syncDir(filepath.Dir(path)); err != nil {
		log.Printf("[state] directory sync failed: %v", err)
	}
}

// writeFileSync is os.WriteFile that returns only once the data is on flash.
func writeFileSync(path string, data []byte, perm os.FileMode) error {
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
