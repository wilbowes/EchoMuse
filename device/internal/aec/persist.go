package aec

import (
	"encoding/binary"
	"errors"
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
	c.loadMicsLocked()
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

// The per-microphone echo paths (micStates) are one file beside the single
// one: a count, then each microphone's saved path with its length, in
// channel order. All or nothing on load, since a file that fits six
// microphones and not the seventh was not written by this filter.
const micsMagic = "EMAEC2"

func (c *Canceller) micsPath() string { return c.statePath + ".mics" }

func (c *Canceller) exportMicsLocked() ([]byte, error) {
	out := append([]byte(micsMagic), 0, 0)
	binary.LittleEndian.PutUint16(out[len(micsMagic):], NumMics)
	for _, st := range c.micStates {
		b, err := c.exportStateLocked(st)
		if err != nil {
			return nil, err
		}
		out = binary.LittleEndian.AppendUint32(out, uint32(len(b)))
		out = append(out, b...)
	}
	return out, nil
}

func (c *Canceller) importMicsLocked(b []byte) error {
	if len(b) < len(micsMagic)+2 || string(b[:len(micsMagic)]) != micsMagic {
		return errors.New("aec: not a set of saved echo paths")
	}
	if n := binary.LittleEndian.Uint16(b[len(micsMagic):]); n != NumMics {
		return errors.New("aec: saved for a different number of microphones")
	}
	b = b[len(micsMagic)+2:]
	var parts [NumMics][]byte
	for i := range parts {
		if len(b) < 4 {
			return errors.New("aec: saved echo paths are truncated")
		}
		n := int(binary.LittleEndian.Uint32(b))
		if n > len(b)-4 {
			return errors.New("aec: saved echo paths are truncated")
		}
		parts[i], b = b[4:4+n], b[4+n:]
		if err := c.checkStateLocked(c.micStates[i], parts[i]); err != nil {
			return err
		}
	}
	if len(b) != 0 {
		return errors.New("aec: saved echo paths have trailing bytes")
	}
	for i, p := range parts {
		if err := c.loadIntoLocked(c.micStates[i], p); err != nil {
			return err
		}
	}
	return nil
}

func (c *Canceller) loadMicsLocked() {
	if c.micStates[0] == nil {
		return
	}
	b, err := os.ReadFile(c.micsPath())
	if err != nil {
		if !os.IsNotExist(err) {
			log.Printf("[aec] saved per-microphone echo paths unreadable, starting cold: %v", err)
		}
		return
	}
	if err := c.importMicsLocked(b); err != nil {
		log.Printf("[aec] saved per-microphone echo paths refused, starting cold: %v", err)
		return
	}
	log.Printf("[aec] loaded the saved echo paths for %d microphones (%d bytes)", NumMics, len(b))
}

// maybeSaveMicsLocked is maybeSaveLocked for the per-microphone paths, on
// the same terms: the window is the active microphone's, and all seven are
// written because all seven adapted on the same playback.
func (c *Canceller) maybeSaveMicsLocked(attDb float64, playing bool) {
	if c.statePath == "" || !c.hwRef || !playing || attDb < saveMinDb {
		return
	}
	if !c.lastMicsSaveTry.IsZero() && time.Since(c.lastMicsSaveTry) < saveEvery {
		return
	}
	c.lastMicsSaveTry = time.Now()
	b, err := c.exportMicsLocked()
	if err != nil {
		return
	}
	go writeState(c.micsPath(), b, attDb)
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

// exportMicsForTest and importMicsForTest take the lock the Locked forms
// expect; nothing in the firmware needs them outside a test.
func (c *Canceller) exportMicsForTest() ([]byte, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.exportMicsLocked()
}

func (c *Canceller) importMicsForTest(b []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.micStates[0] == nil {
		return errors.New("aec: no per-microphone states")
	}
	return c.importMicsLocked(b)
}
