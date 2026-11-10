package server

import (
	"encoding/json"
	"math"
	"os"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
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

// One ring state per button step, as stock: 12 LEDs at three brightnesses,
// so every press visibly moves the arc and none moves it twice.
func TestTheBandHasOneLevelPerRingState(t *testing.T) {
	if arcStates != numLEDs*3 {
		t.Fatalf("%d grid levels for %d ring states", arcStates, numLEDs*3)
	}
	if volumeButtonFloor+volumeSteps*volumeStep != volumeMax {
		t.Fatalf("the grid from %d in steps of %d misses unity", volumeButtonFloor, volumeStep)
	}
	if volumeStep != 2 {
		t.Fatalf("step %d levels, want 2 (1dB)", volumeStep)
	}
}

func TestStepsStayInsideTheButtonBand(t *testing.T) {
	f, s := volumeButtonFloor, volumeStep
	cases := []struct {
		name    string
		in, dir int
		want    int
	}{
		// A level below the floor (HA can set one, and so can a stored level)
		// reaches audible in ONE press, whichever button.
		{"far below the floor, up", f - 40, +1, f},
		{"far below the floor, down", f - 40, -1, f},
		{"just below the floor, up", f - 1, +1, f},
		{"on the grid, up", f + 4*s, +1, f + 5*s},
		{"on the grid, down", f + 4*s, -1, f + 3*s},
		// Between steps (a level from before the grid) snaps to a neighbour.
		{"between steps, up", f + 4*s + 1, +1, f + 5*s},
		{"between steps, down", f + 4*s + 1, -1, f + 4*s},
		{"above the ceiling, up", volumeMax + 30, +1, volumeMax},
		{"above the ceiling, down", volumeMax + 30, -1, volumeMax},
	}
	for _, tc := range cases {
		if got := stepLevel(tc.in, tc.dir); got != tc.want {
			t.Errorf("%s: stepLevel(%d, %d) = %d, want %d",
				tc.name, tc.in, tc.dir, got, tc.want)
		}
	}
}

// Stepping up from the top and down from the bottom must settle, not
// oscillate or run away past the band; and floor to unity takes exactly
// volumeSteps presses, each lighting the next ring state.
func TestSteppingCrossesTheBandOneStateAtATime(t *testing.T) {
	level := volumeButtonFloor
	if arcState(level) != 1 {
		t.Fatalf("the floor shows state %d, want 1", arcState(level))
	}
	for i := 1; i <= volumeSteps; i++ {
		level = stepLevel(level, +1)
		if arcState(level) != i+1 {
			t.Fatalf("press %d: level %d shows state %d, want %d", i, level, arcState(level), i+1)
		}
	}
	if level != volumeMax || stepLevel(level, +1) != volumeMax {
		t.Errorf("stepping up settled at %d, want %d", level, volumeMax)
	}
	for i := 0; i < volumeSteps+5; i++ {
		level = stepLevel(level, -1)
	}
	if level != volumeButtonFloor {
		t.Errorf("stepping down settled at %d, want %d", level, volumeButtonFloor)
	}
}

// Home Assistant (via the controller) and Sendspin give a percentage. Every
// one lands on a level some number of presses reaches, so the ring reads the
// same whichever way the volume got there; 0 stays silent and 1 is the floor.
func TestPercentLandsOnTheButtonGrid(t *testing.T) {
	if PercentToLevel(0) != 0 || PercentToLevel(1) != volumeButtonFloor || PercentToLevel(100) != volumeMax {
		t.Fatalf("endpoints: 0->%d 1->%d 100->%d", PercentToLevel(0), PercentToLevel(1), PercentToLevel(100))
	}
	prev := 0
	for pct := 1; pct <= 100; pct++ {
		level := PercentToLevel(float64(pct))
		if (level-volumeButtonFloor)%volumeStep != 0 {
			t.Errorf("%d%% -> level %d, off the grid", pct, level)
		}
		if level < prev {
			t.Errorf("%d%% -> %d, below %d for less", pct, level, prev)
		}
		prev = level
		// A level reported back lands on the same level again.
		if back := PercentToLevel(LevelToPercent(level)); back != level {
			t.Errorf("level %d -> %.2f%% -> %d", level, LevelToPercent(level), back)
		}
	}
	if LevelToPercent(0) != 0 || LevelToPercent(1) != 1 || LevelToPercent(volumeButtonFloor) != 1 {
		t.Error("silent must read 0 and anything audible at least 1")
	}
}

func TestArcLightsThreeStatesPerLED(t *testing.T) {
	sum := func(leds []led.Led) (n int, g []uint8) {
		for _, l := range leds {
			if l.G > 0 {
				n++
			}
			g = append(g, l.G)
		}
		return
	}
	if n, _ := sum(arcLEDs(0)); n != 0 {
		t.Errorf("state 0 lights %d LEDs", n)
	}
	for state := 1; state <= arcStates; state++ {
		n, g := sum(arcLEDs(state))
		if want := (state + 2) / 3; n != want {
			t.Errorf("state %d lights %d LEDs, want %d", state, n, want)
		}
		if g[(state-1)/3] != arcLevels[(state-1)%3+1] {
			t.Errorf("state %d: leading LED at %d", state, g[(state-1)/3])
		}
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

// A server's volume command must come back as the same number, or Music
// Assistant and the Echo correct each other forever.
func TestPercentEchoReturnsTheCommand(t *testing.T) {
	var e PercentEcho
	for pct := 0; pct <= 100; pct++ {
		if got := e.ToPercent(e.ToLevel(pct)); got != pct {
			t.Errorf("%d%% -> %d -> %d%%", pct, e.ToLevel(pct), got)
		}
	}
	// A level reached some other way (a button) reports its own percentage.
	e.ToLevel(50)
	if got := e.ToPercent(volumeMax); got != 100 {
		t.Errorf("unity reads %d%%", got)
	}
}

// The controller maps Home Assistant's volume with the same law
// (em_volume.ha_volume_to_device); controller/tests/test_volume.py writes
// testdata/volume_law.json from it, and this holds the Go to it.
func TestTheLawMatchesTheControllers(t *testing.T) {
	raw, err := os.ReadFile("testdata/volume_law.json")
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		PercentToLevel []int     `json:"percent_to_level"`
		LevelToPercent []float64 `json:"level_to_percent"`
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	for pct, want := range v.PercentToLevel {
		if got := PercentToLevel(float64(pct)); got != want {
			t.Errorf("PercentToLevel(%d) = %d, controller says %d", pct, got, want)
		}
	}
	for level, want := range v.LevelToPercent {
		if got := LevelToPercent(level); math.Abs(got-want) > 1e-5 {
			t.Errorf("LevelToPercent(%d) = %f, controller says %f", level, got, want)
		}
	}
}
