package aec

import (
	"encoding/binary"
	"math"
	"os"
	"testing"
	"time"
)

// hwCanceller is a canceller on the hardware-reference path, as the device
// runs it. tailMs is the configured (software tap) length, which the
// hardware path does not use.
func hwCanceller(tailMs int) *Canceller {
	c := New()
	c.SetParams(true, 0, tailMs)
	c.SetHardwareRef(true)
	return c
}

// firstFramesAttenuation runs the echo scenario from
// TestHardwareRefCancelsWithoutRingOrDelay (33 samples, inverted) and
// returns the cancellation over the first n frames, in dB.
func firstFramesAttenuation(c *Canceller, signal []int16, n int) float64 {
	var in, out float64
	for f := 0; f < n; f++ {
		lo, hi := f*FrameSize, (f+1)*FrameSize
		mic := make([]int16, FrameSize)
		for i := range mic {
			if j := lo + i - 33; j >= 0 {
				mic[i] = -signal[j]
			}
		}
		mb := toBytes(mic)
		res := c.ProcessWithRef(mb, toBytes(signal[lo:hi]))
		in += rms(mb)
		out += rms(res)
	}
	return 20 * math.Log10(in/out)
}

// The point of saving the echo path: a canceller loaded with one cancels
// from its first frames, where a cold one is still learning. Measured on
// the bench as 0-1dB for the first seconds of a reply from cold (#aec
// harness, 2026-09-22).
func TestLoadedStateCancelsFromTheFirstFrame(t *testing.T) {
	signal := synth(200 * FrameSize)
	warm := hwCanceller(64)
	firstFramesAttenuation(warm, signal, 200)
	saved, err := warm.ExportState()
	if err != nil {
		t.Fatal(err)
	}

	other := synth(20*FrameSize + 7) // different audio from the one learnt on
	other = other[7:]
	cold := firstFramesAttenuation(hwCanceller(64), other, 10)
	loaded := hwCanceller(64)
	if err := loaded.ImportState(saved); err != nil {
		t.Fatal(err)
	}
	warmStart := firstFramesAttenuation(loaded, other, 10)
	t.Logf("first 10 frames: cold %.1fdB, loaded %.1fdB", cold, warmStart)
	if warmStart < cold+10 {
		t.Fatalf("loaded state did not help: cold %.1fdB, loaded %.1fdB", cold, warmStart)
	}
}

func TestImportRefusesAMismatchedFilter(t *testing.T) {
	saved, _ := hwCanceller(300).ExportState()
	sw := New()
	sw.SetParams(true, 0, 300) // software tap: a 300ms filter
	if err := sw.ImportState(saved); err == nil {
		t.Fatal("a hardware-path state must not load into a software-tap filter")
	}
}

// The hardware path runs at hwTailMs whatever aecTailMs says, and a tail
// change pushed while it runs must not discard the converged filter.
func TestHardwarePathUsesItsOwnTail(t *testing.T) {
	c := hwCanceller(300)
	if c.stTailMs != hwTailMs {
		t.Fatalf("hardware path built at %dms, want %d", c.stTailMs, hwTailMs)
	}
	st := c.st
	c.SetParams(true, 0, 200)
	if c.st != st {
		t.Fatal("a tail change on the hardware path rebuilt the filter")
	}
	if c.tailMs != 200 {
		t.Fatal("the new tail must still be stored for the software tap")
	}
	c.SetHardwareRef(false)
	if c.stTailMs != 200 {
		t.Fatalf("falling back to the software tap built at %dms, want 200", c.stTailMs)
	}
}

func TestImportRefusesNonFiniteValues(t *testing.T) {
	c := hwCanceller(64)
	saved, _ := c.ExportState()
	binary.LittleEndian.PutUint32(saved[len(saved)-4:], math.Float32bits(float32(math.NaN())))
	if err := hwCanceller(64).ImportState(saved); err == nil {
		t.Fatal("a NaN in the saved filter must be refused")
	}
}

func TestImportRefusesGarbage(t *testing.T) {
	c := hwCanceller(64)
	for _, b := range [][]byte{nil, []byte("EMAEC1"), []byte("not a state at all, clearly")} {
		if err := c.ImportState(b); err == nil {
			t.Fatalf("accepted %q", b)
		}
	}
}

func TestExportNeedsARunningCanceller(t *testing.T) {
	if _, err := New().ExportState(); err == nil {
		t.Fatal("export from a disabled canceller must fail")
	}
}

// A converged hardware-path filter saves itself, and a fresh canceller given
// the same path loads it the moment the hardware reference is confirmed.
func TestEchoPathIsSavedAndLoadedAcrossARestart(t *testing.T) {
	path := t.TempDir() + "/aec_echo_path.bin"
	signal := synth(200 * FrameSize)

	first := New()
	first.SetStatePath(path)
	first.SetParams(true, 0, 300)
	first.SetHardwareRef(true)
	firstFramesAttenuation(first, signal, 200)
	var saved []byte
	for i := 0; i < 200 && saved == nil; i++ { // the write runs on its own goroutine
		saved, _ = os.ReadFile(path)
		time.Sleep(5 * time.Millisecond)
	}
	if saved == nil {
		t.Fatal("a converged filter was not saved")
	}

	restarted := New()
	restarted.SetStatePath(path)
	restarted.SetParams(true, 0, 300)
	restarted.SetHardwareRef(true)
	other := synth(20*FrameSize + 7)[7:]
	if att := firstFramesAttenuation(restarted, other, 10); att < 30 {
		t.Fatalf("restart did not start from the saved echo path: %.1fdB over the first frames", att)
	}
}

// Saving is rate-limited: a second converged window inside a day writes
// nothing, which is what keeps this off the flash on every reply.
func TestEchoPathIsNotRewrittenWithinADay(t *testing.T) {
	path := t.TempDir() + "/aec_echo_path.bin"
	if err := os.WriteFile(path, []byte("recent"), 0o600); err != nil {
		t.Fatal(err)
	}
	c := New()
	c.SetStatePath(path)
	c.SetParams(true, 0, 300)
	c.SetHardwareRef(true)
	firstFramesAttenuation(c, synth(200*FrameSize), 200)
	time.Sleep(50 * time.Millisecond)
	if b, _ := os.ReadFile(path); string(b) != "recent" {
		t.Fatal("a file younger than a day was rewritten")
	}
}

// A software-tap filter's alignment depends on runtime delay and would not
// transfer, so it is never saved.
func TestSoftwareTapNeverSaves(t *testing.T) {
	path := t.TempDir() + "/aec_echo_path.bin"
	c := New()
	c.SetStatePath(path)
	c.SetParams(true, 0, 300)
	c.maybeSaveLocked(30, true)
	time.Sleep(20 * time.Millisecond)
	if _, err := os.Stat(path); err == nil {
		t.Fatal("the software tap saved its filter")
	}
}
