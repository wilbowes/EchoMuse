package server

import (
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// #990: "No wake word" gives the idle ring every other LED dim red, painted
// by the device. Mute outranks it, a turn paints normally, and the idle
// pattern comes back after the turn and after unmute.
func TestNoWakeWordRingIsIdleOnly(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	s.SetWakeWordOff(true)
	assertNoWakeWordFrame(t, lastFrame(ring), "at idle")

	s.SetLEDs(frame(0, 180, 0), nil)
	if got := ring.last(); got.R != 0 || got.G != 180 {
		t.Fatalf("turn frame did not paint over the idle ring: %+v", got)
	}

	s.StartAnim(AnimSpec{Pattern: "off"})
	assertNoWakeWordFrame(t, lastFrame(ring), "after the turn")

	s.mute.mu.Lock()
	s.mute.muted = true
	s.mute.mu.Unlock()
	painted := ringFrameCount(ring)
	s.SetWakeWordOff(false)
	s.SetWakeWordOff(true)
	if ringFrameCount(ring) != painted {
		t.Fatal("a muted device repainted the ring")
	}
	s.mute.showMuteLEDs()
	if got := ring.last(); got.R != 180 || got.G != 0 || got.B != 0 {
		t.Fatalf("mute ring did not win: %+v", got)
	}

	s.mute.mu.Lock()
	s.mute.muted = false
	s.mute.mu.Unlock()
	s.restoreAfterUnmute()
	assertNoWakeWordFrame(t, lastFrame(ring), "after unmute")

	s.SetWakeWordOff(false)
	if got := ring.last(); got.R != 0 || got.G != 0 || got.B != 0 {
		t.Fatalf("idle ring did not go dark with the wake word back on: %+v", got)
	}
}

func lastFrame(r *recordingRing) []led.Led {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]led.Led(nil), r.frames[len(r.frames)-1]...)
}

func assertNoWakeWordFrame(t *testing.T, got []led.Led, when string) {
	t.Helper()
	if len(got) != 12 {
		t.Fatalf("%s: got %d LEDs, want 12", when, len(got))
	}
	for i, l := range got {
		if i%2 == 0 {
			if l.R == 0 || l.R >= 180 || l.G != 0 || l.B != 0 {
				t.Fatalf("%s: LED %d = %+v, want dim red", when, i, l)
			}
			continue
		}
		if l.R != 0 || l.G != 0 || l.B != 0 {
			t.Fatalf("%s: LED %d = %+v, want dark", when, i, l)
		}
	}
}
