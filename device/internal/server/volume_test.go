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

// radarVolumeSteps is stock's VolumeCurves.xml music row through stock's
// mixer level table — recomputed here from both, so a typo in either the
// ladder or the derivation fails. Pinned against controller/em_volume's
// STOCK_MIXER_LEVELS too (same table, value + 27 from 11 up).
func TestRadarVolumeStepsAreStocks(t *testing.T) {
	curve := []int{0, 1, 2, 3, 4, 6, 11, 16, 22, 28, 34, 40, 44, 46, 50, 54,
		56, 60, 64, 68, 70, 72, 76, 80, 84, 88, 90, 92, 96, 98, 100}
	low := []int{0, 3, 7, 11, 17, 20, 27, 30, 32, 35, 36}
	mixer := func(v int) int {
		if v <= 10 {
			return low[v]
		}
		return v + 27
	}
	if len(radarVolumeSteps) != 30 {
		t.Fatalf("%d steps, want 30 (stock's volume_step-01..30)", len(radarVolumeSteps))
	}
	for s := 1; s <= 30; s++ {
		if got, want := radarVolumeSteps[s-1], mixer(curve[s]); got != want {
			t.Errorf("step %d: level %d, want %d", s, got, want)
		}
	}
}

// On Radar the buttons walk stock's ladder: 29 presses from the bottom step
// reach unity, a level between steps lands on the next one, and neither end
// steps past itself.
func TestRadarButtonsWalkStocksSteps(t *testing.T) {
	vc := &volumeController{ledCtrl: func() led.Controller { return nil }, steps: radarVolumeSteps}
	vc.Set(0, false)
	vc.StepUp()
	if vc.Get() != 3 {
		t.Fatalf("first press from silence: %d, want 3", vc.Get())
	}
	for i := 0; i < 29; i++ {
		vc.StepUp()
	}
	if vc.Get() != 127 {
		t.Fatalf("after 30 presses: %d, want 127", vc.Get())
	}
	vc.StepUp()
	if vc.Get() != 127 {
		t.Fatalf("past the top: %d", vc.Get())
	}
	vc.Set(80, false) // between 77 and 81
	vc.StepUp()
	if vc.Get() != 81 {
		t.Errorf("up from 80: %d, want 81", vc.Get())
	}
	vc.Set(80, false)
	vc.StepDown()
	if vc.Get() != 77 {
		t.Errorf("down from 80: %d, want 77", vc.Get())
	}
	vc.Set(3, false)
	vc.StepDown()
	if vc.Get() != 3 {
		t.Errorf("down from the bottom step: %d, want 3", vc.Get())
	}
}

func TestStepIndex(t *testing.T) {
	for _, tc := range []struct{ level, idx int }{{0, 0}, {2, 0}, {3, 1}, {80, 14}, {81, 15}, {127, 30}} {
		if got := stepIndex(radarVolumeSteps, tc.level); got != tc.idx {
			t.Errorf("level %d: index %d, want %d", tc.level, got, tc.idx)
		}
	}
}

// The arc on stock's 30-step ladder moves on nearly every press, and the
// LED a step has only partly reached is at half brightness.
func TestStepArcHalfBrightnessBetweenLEDs(t *testing.T) {
	const n = 12
	arc := func(idx int) []int { return stepArc(radarVolumeSteps, radarVolumeSteps[idx-1], n) }
	sum := func(a []int) (t int) {
		for _, v := range a {
			t += v
		}
		return
	}
	// Step 1 is a single half-bright LED, step 2 fills it, step 3 starts the next.
	for idx, want := range map[int][]int{
		1:  {1},
		2:  {2},
		3:  {2, 1},
		5:  {2, 2},
		30: {2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2},
	} {
		got := arc(idx)
		for i, w := range want {
			if got[i] != w {
				t.Errorf("step %d LED %d = %d, want %d (arc %v)", idx, i, got[i], w, got)
			}
		}
		for i := len(want); i < n; i++ {
			if got[i] != 0 {
				t.Errorf("step %d LED %d = %d, want off (arc %v)", idx, i, got[i], got)
			}
		}
	}
	// Never lights past the front: at most one LED is half-bright, and every
	// LED before it is full.
	moves, prev := 0, 0
	for idx := 1; idx <= len(radarVolumeSteps); idx++ {
		a := arc(idx)
		halves, seenOff := 0, false
		for _, v := range a {
			if v == 1 {
				halves++
			}
			if seenOff && v != 0 {
				t.Errorf("step %d: lit LED after an off one: %v", idx, a)
			}
			if v < 2 {
				seenOff = true
			}
		}
		if halves > 1 {
			t.Errorf("step %d: %d half-bright LEDs: %v", idx, halves, a)
		}
		if s := sum(a); s < prev {
			t.Errorf("step %d: arc shrank %d -> %d", idx, prev, s)
		} else {
			if s > prev {
				moves++
			}
			prev = s
		}
	}
	// Whole LEDs alone changed the ring on 12 of 30 presses; with the half
	// step it changes on 24.
	if moves != 24 {
		t.Errorf("ring changes on %d of 30 steps, want 24", moves)
	}
	// Below the ladder: one half-bright LED, never dark above silence.
	if a := stepArc(radarVolumeSteps, 1, n); a[0] != 1 || sum(a) != 1 {
		t.Errorf("below the bottom step: %v, want one half-bright LED", a)
	}
}
