package server

import (
	"github.com/wilbowes/EchoMuse/pkg/led"
	"testing"
	"time"
)

// A deliberate button press must outrank the volume arc's 2s hold. Before
// this, adjusting volume then immediately pressing the action button left
// the arc owning the ring for the remainder of its window, so the device
// gave no sign it had started listening.
func TestCancelDisplayReleasesTheRing(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })

	vc.mu.Lock()
	vc.displayActive = true
	vc.timer = time.AfterFunc(volumeLEDSecs*time.Second, func() {})
	vc.mu.Unlock()

	if !vc.DisplayActive() {
		t.Fatal("precondition: arc should own the ring")
	}

	vc.CancelDisplay()

	if vc.DisplayActive() {
		t.Fatal("arc still owns the ring after CancelDisplay — a listening " +
			"frame would be recorded but not painted")
	}
	// Idempotent: a second press must not panic on the already-stopped timer.
	vc.CancelDisplay()
}

// tinymix ctl 61 spans 0..175, but 127 is the codec's 0dB. Above it the DAC
// applies positive digital gain to near-full-scale PCM and saturates —
// measured on hardware at 65% THD by index 153, 89% by 170, with the output
// level flat from 153 up because it had stopped getting louder. Stock FireOS
// never writes this control at all. If this constant creeps back toward 175,
// the garbling above ~73% volume returns.
func TestVolumeMaxIsCodecUnityNotTheControlMaximum(t *testing.T) {
	if volumeMax != 127 {
		t.Fatalf("volumeMax = %d, want 127 (0dB). Anything higher clips the DAC.",
			volumeMax)
	}
	if volumeButtonFloor >= volumeMax {
		t.Fatalf("button floor %d must sit below the ceiling %d",
			volumeButtonFloor, volumeMax)
	}
}

// The button band must be crossable in a sane number of presses: too few and
// each press is a huge jump, too many and reaching the top is a chore.
func TestButtonBandTakesAReasonableNumberOfPresses(t *testing.T) {
	presses := (volumeMax - volumeButtonFloor) / volumeStep
	if presses < 6 || presses > 16 {
		t.Fatalf("%d presses to cross the band (step %d over %d..%d); "+
			"want roughly 8-12", presses, volumeStep, volumeButtonFloor, volumeMax)
	}
}

func TestStepsStayInsideTheButtonBand(t *testing.T) {
	cases := []struct {
		name string
		in   int
		want int
	}{
		// A level below the floor — HA can set one, and so could a stored
		// level from before the cap — must reach audible in ONE press, not
		// creep up 4dB at a time through inaudible territory.
		{"far below the floor lands on it", volumeButtonFloor - 40, volumeButtonFloor},
		{"just below the floor lands on it", volumeButtonFloor - 1, volumeButtonFloor},
		{"inside the band is untouched", volumeButtonFloor + volumeStep, volumeButtonFloor + volumeStep},
		{"above the ceiling clamps down", volumeMax + 30, volumeMax},
	}
	for _, tc := range cases {
		if got := clampToButtonBand(tc.in); got != tc.want {
			t.Errorf("%s: clampToButtonBand(%d) = %d, want %d",
				tc.name, tc.in, got, tc.want)
		}
	}
}

// Stepping up from the top and down from the bottom must settle, not
// oscillate or run away past the band.
func TestSteppingSaturatesAtBothEnds(t *testing.T) {
	level := volumeMax
	for i := 0; i < 5; i++ {
		level = clampToButtonBand(level + volumeStep)
	}
	if level != volumeMax {
		t.Errorf("stepping up from the ceiling reached %d, want %d", level, volumeMax)
	}

	level = volumeButtonFloor
	for i := 0; i < 5; i++ {
		level = clampToButtonBand(level - volumeStep)
	}
	if level != volumeButtonFloor {
		t.Errorf("stepping down from the floor reached %d, want %d",
			level, volumeButtonFloor)
	}
}

func TestStepReportsWhetherTheLevelChanged(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })
	vc.Set(volumeMax, false)
	if vc.StepUp() {
		t.Fatal("step up at the ceiling reported a change")
	}
	if !vc.StepDown() {
		t.Fatal("step down from the ceiling did not report a change")
	}
	vc.Set(volumeButtonFloor, false)
	if vc.StepDown() {
		t.Fatal("step down at the floor reported a change")
	}
}
