package cue

import (
	"math"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/profile"
)

func testProfile() *profile.Profile {
	p := &profile.Profile{}
	p.Speaker.SampleRate = 48000
	p.Speaker.PeriodSize = 1536
	p.Speaker.Channels = 2
	p.WakeCue = profile.WakeCue{
		Enabled:        true,
		ChimeMs:        120,
		ChimeHz:        880,
		ChimeAmplitude: 0.18,
	}
	return p
}

func samples(b []byte) []int16 {
	out := make([]int16, len(b)/2)
	for i := range out {
		out[i] = int16(uint16(b[i*2]) | uint16(b[i*2+1])<<8)
	}
	return out
}

// dominantHz estimates the strongest frequency in a block by counting zero
// crossings, enough to tell two notes a major third apart from each other
// without pulling in an FFT.
func dominantHz(s []int16, rate float64) float64 {
	var crossings int
	for i := 1; i < len(s); i++ {
		if (s[i-1] < 0) != (s[i] < 0) {
			crossings++
		}
	}
	return float64(crossings) / 2 * rate / float64(len(s))
}

func TestChimeLength(t *testing.T) {
	p := testProfile()
	got := renderChime(p, true)
	// Two notes of ChimeMs/2 each, mono S16.
	want := 2 * int(float64(p.Speaker.SampleRate)*float64(p.WakeCue.ChimeMs/2)/1000) * 2
	if len(got) != want {
		t.Errorf("chime = %d bytes, want %d", len(got), want)
	}
}

// TestChimeStartsAndEndsSilent is the anti-click test. A bare sine switched on
// and off steps the DAC from zero to full amplitude in one sample, and on this
// speaker that click is louder than the tone it precedes. The raised-cosine
// envelope must bring both ends to (near) zero.
func TestChimeStartsAndEndsSilent(t *testing.T) {
	for _, rising := range []bool{true, false} {
		s := samples(renderChime(testProfile(), rising))
		if len(s) == 0 {
			t.Fatal("empty chime")
		}
		var peak int16
		for _, v := range s {
			if v > peak {
				peak = v
			}
		}
		limit := int16(float64(peak) * 0.02)
		if abs16(s[0]) > limit {
			t.Errorf("rising=%v: first sample %d exceeds 2%% of peak %d, will click",
				rising, s[0], peak)
		}
		if abs16(s[len(s)-1]) > limit {
			t.Errorf("rising=%v: last sample %d exceeds 2%% of peak %d, will click",
				rising, s[len(s)-1], peak)
		}
	}
}

// TestChimeDirection checks that start and end are actually distinguishable:
// the rising cue must go up in pitch and the falling one down. If both were
// rendered the same way the user cannot tell "listening" from "done".
func TestChimeDirection(t *testing.T) {
	p := testProfile()
	rate := float64(p.Speaker.SampleRate)

	for _, tc := range []struct {
		name   string
		rising bool
	}{{"rising", true}, {"falling", false}} {
		s := samples(renderChime(p, tc.rising))
		half := len(s) / 2
		first := dominantHz(s[:half], rate)
		second := dominantHz(s[half:], rate)

		if tc.rising && second <= first {
			t.Errorf("rising chime: %0.f Hz -> %0.f Hz, expected to go up", first, second)
		}
		if !tc.rising && second >= first {
			t.Errorf("falling chime: %0.f Hz -> %0.f Hz, expected to go down", first, second)
		}
	}
}

func TestChimeRespectsAmplitude(t *testing.T) {
	p := testProfile()
	p.WakeCue.ChimeAmplitude = 0.1
	s := samples(renderChime(p, true))

	var peak int16
	for _, v := range s {
		if a := abs16(v); a > peak {
			peak = a
		}
	}
	want := 0.1 * math.MaxInt16
	// The envelope means the peak is close to, but never above, amplitude.
	if float64(peak) > want*1.02 {
		t.Errorf("peak %d exceeds requested amplitude %.0f", peak, want)
	}
	if float64(peak) < want*0.8 {
		t.Errorf("peak %d well below requested amplitude %.0f, envelope too aggressive", peak, want)
	}
}

// TestNewDisabledReturnsNil covers the biscuit case: a device whose LED ring
// already signals wake must get no Cue at all, and every method has to be safe
// on the nil result.
func TestNewDisabledReturnsNil(t *testing.T) {
	p := testProfile()
	p.WakeCue.Enabled = false
	c := New(p, nil)
	if c != nil {
		t.Fatal("New should return nil when WakeCue is disabled")
	}
	// These run on the control-plane goroutine; a panic here would take out
	// LED handling for the whole session.
	c.Start()
	c.Stop()
}

// TestStartStopAreEdgeTriggered guards the behaviour that matters in practice:
// the controller repaints the listening frame ~15 times a second, and without
// edge detection every repaint would re-chime.
func TestStartStopAreEdgeTriggered(t *testing.T) {
	p := testProfile()
	p.WakeCue.ChimeMs = 0 // no audio, no speaker needed
	c := New(p, nil)
	if c == nil {
		t.Fatal("expected a Cue")
	}

	c.Start()
	if !c.listening {
		t.Fatal("Start did not mark the cue as listening")
	}
	first := c.turn
	c.Start() // repeat, as the controller would
	if c.turn != first {
		t.Errorf("second Start advanced the turn counter %d -> %d; it should be ignored",
			first, c.turn)
	}

	c.Stop()
	if c.listening {
		t.Fatal("Stop did not clear the listening state")
	}
	c.Stop() // repeat
	if c.listening {
		t.Error("second Stop should remain a no-op")
	}
}

func abs16(v int16) int16 {
	if v < 0 {
		return -v
	}
	return v
}
