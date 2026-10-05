package aec

import (
	"log"
	"os"
	"time"
)

// Saving the learned echo path across restarts (see ExportState).
//
// Loaded when the hardware-reference filter is built, which is when the
// first playback after boot confirms ch8. Saved from the attenuation
// telemetry: after a one-second playback window cancelling at least
// saveMinDb, at most once a day, because this runs for years on eMMC that
// cannot be replaced. Only the hardware path saves: the software tap's
// alignment depends on runtime delay, so its filter would not transfer.
//
// A saved path that does not load (wrong shape, corrupt, a firmware with a
// different filter) costs a cold start, which is what every boot was before.

const (
	// Converged cancellation measured ~20dB and cold 0-2dB on the bench
	// (2026-09-22), so 12dB says the filter has learnt this echo path.
	saveMinDb = 12.0
	// Minimum age before the file is rewritten, by wall clock when the file
	// exists and by uptime within a run.
	saveEvery = 24 * time.Hour
)

// SetStatePath enables saving and loading the echo path at path. Empty
// disables both.
func (c *Canceller) SetStatePath(path string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.statePath = path
}

// loadStateLocked loads the saved echo path into a freshly built
// hardware-path filter. One small file read, once per boot.
func (c *Canceller) loadStateLocked() {
	if c.statePath == "" || !c.hwRef || c.st == nil {
		return
	}
	b, err := os.ReadFile(c.statePath)
	if err != nil {
		if !os.IsNotExist(err) {
			log.Printf("[aec] saved echo path unreadable, starting cold: %v", err)
		}
		return
	}
	// importLocked, not ImportState: the lock is already held.
	if err := c.importLocked(b); err != nil {
		log.Printf("[aec] saved echo path refused, starting cold: %v", err)
		return
	}
	log.Printf("[aec] loaded the saved echo path (%d bytes)", len(b))
}

// maybeSaveLocked is called once per attenuation window. The export is a
// 16KB copy under the lock; the file write runs on its own goroutine so the
// mic goroutine never waits on flash.
func (c *Canceller) maybeSaveLocked(attDb float64, playing bool) {
	if c.statePath == "" || !c.hwRef || !playing || attDb < saveMinDb {
		return
	}
	if !c.lastSaveTry.IsZero() && time.Since(c.lastSaveTry) < saveEvery {
		return
	}
	c.lastSaveTry = time.Now()
	b, err := c.exportLocked()
	if err != nil {
		return
	}
	go writeState(c.statePath, b, attDb)
}

func writeState(path string, b []byte, attDb float64) {
	// A file younger than a day is left alone, which also covers the first
	// window after every boot. A clock still reading 2010 makes a real file
	// look younger than it is, so the rewrite waits for the time sync rather
	// than happening early.
	if fi, err := os.Stat(path); err == nil {
		if age := time.Since(fi.ModTime()); age >= 0 && age < saveEvery {
			return
		}
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		log.Printf("[aec] saving the echo path: %v", err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		log.Printf("[aec] saving the echo path: %v", err)
		return
	}
	log.Printf("[aec] saved the echo path (%.1fdB cancellation, %d bytes)", attDb, len(b))
}

// DefaultStatePath is where the device keeps the saved echo path, beside
// the other files that outlive a process and an OTA.
const DefaultStatePath = "/data/local/etc/echomuse/aec_echo_path.bin"
