package server

import (
	"math"
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// The curve is the whole setting: a linear multiply would make "50%" look
// like 73% and the slider would feel broken at the top of its range. These
// are the numbers that make the percentage mean what the eye sees.
func TestRingScaleIsPerceptualWithExactEnds(t *testing.T) {
	if got := ringScale(100); got != 1 {
		t.Fatalf("ringScale(100) = %v, want exactly 1 — full brightness must "+
			"render bit-identically to the pre-#295 frames", got)
	}
	if got := ringScale(0); got != 0 {
		t.Fatalf("ringScale(0) = %v, want 0 — the ring must be able to be off", got)
	}
	// Out of range is clamped by the caller, but the function is total.
	if got := ringScale(-40); got != 0 {
		t.Fatalf("ringScale(-40) = %v, want 0", got)
	}
	if got := ringScale(140); got != 1 {
		t.Fatalf("ringScale(140) = %v, want 1", got)
	}

	// 0.5^2.2 ≈ 0.218, NOT 0.5. Pinned so a change of exponent is a visible
	// decision, in both places it would have to be made.
	half := ringScale(50)
	if half < 0.20 || half > 0.24 {
		t.Fatalf("ringScale(50) = %.3f, want ~0.218 (50%% ** 2.2). A linear "+
			"multiply would read as 73%% brightness, which is why the gamma "+
			"exists", half)
	}

	prev := -1.0
	for p := 0; p <= 100; p += 5 {
		got := ringScale(float64(p))
		if got <= prev {
			t.Fatalf("ringScale is not strictly increasing: %d%% = %.4f after %.4f",
				p, got, prev)
		}
		prev = got
	}
}

func TestBrightnessControllerDimsEveryFrameAndKeepsIDs(t *testing.T) {
	ring := &recordingRing{}
	percent := 50.0
	dimmed := brightnessController{
		Controller: ring,
		percent:    func() float64 { return percent },
	}
	in := []led.Led{{ID: 3, R: 200, G: 100, B: 0}, {ID: 7, R: 0, G: 255, B: 128}}
	if err := dimmed.SetLEDs(in...); err != nil {
		t.Fatalf("SetLEDs: %v", err)
	}

	scale := ringScale(percent)
	ring.mu.Lock()
	got := ring.frames[len(ring.frames)-1]
	ring.mu.Unlock()
	for i, l := range got {
		if l.ID != in[i].ID {
			t.Errorf("LED %d: ID = %d, want %d — IDs address the ring, not "+
				"the frame order", i, l.ID, in[i].ID)
		}
		want := func(v uint8) uint8 { return uint8(math.Round(float64(v) * scale)) }
		if l.R != want(in[i].R) || l.G != want(in[i].G) || l.B != want(in[i].B) {
			t.Errorf("LED %d = %+v, want each channel scaled by %.3f", i, l, scale)
		}
	}
}

// The red mute ring is exempt (#295): at brightness 0 every other frame is
// black, and "the mic is muted" must still be readable across a room.
func TestTheMuteRingIgnoresTheBrightnessSetting(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	s.SetLEDBrightness(0)
	s.SetLEDs(frame(90, 90, 90), nil)
	if got := ring.last(); got.R != 0 || got.G != 0 || got.B != 0 {
		t.Fatalf("at 0%% a normal frame is not off: %+v", got)
	}

	s.mute.mu.Lock()
	s.mute.muted = true
	s.mute.mu.Unlock()
	s.RestoreMuteRing()
	if got := ring.last(); got.R != 180 || got.G != 0 || got.B != 0 {
		t.Fatalf("mute ring at 0%% brightness = %+v, want full red — the one "+
			"ring a display setting must not be able to hide", got)
	}
}

// A static ring has no next frame to wait for, so the setter repaints.
func TestChangingTheBrightnessRepaintsTheStoredFrame(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	s.SetLEDs(frame(100, 100, 100), nil)
	before := ringFrameCount(ring)
	s.SetLEDBrightness(50)
	if got := ringFrameCount(ring); got != before+1 {
		t.Fatalf("brightness change painted %d frames, want one repaint", got-before)
	}
	want := uint8(math.Round(100 * ringScale(50)))
	if got := ring.last(); got.R != want {
		t.Fatalf("repainted frame R = %d, want %d at 50%%", got.R, want)
	}
	// Out of range clamps rather than scaling past the curve.
	s.SetLEDBrightness(500)
	if got := s.ledBrightnessPercent(); got != 100 {
		t.Fatalf("brightness 500 stored as %v, want 100", got)
	}
}
