package server

import (
	"math"
	"testing"
)

// The old mapping, kept here to hold biscuit to it: round to the nearest 30
// degrees, then count from LED 0 at 240.
func oldBiscuitLED(angleDeg float64) int {
	norm := int(math.Round(angleDeg/30)) * 30
	return ((norm - 240 + 360) % 360) / 30 % 12
}

func TestRingLEDForKeepsBiscuitsMapping(t *testing.T) {
	for a := 0.0; a < 360; a += 1 {
		if got, want := ringLEDFor(a, 240), oldBiscuitLED(a); got != want {
			t.Errorf("%v degrees: LED %d, was %d", a, got, want)
		}
	}
}

// The Dot 3's microphones are on the hours and its LEDs straddle them, LED 0
// just clockwise of 6 o'clock (41C8, 2026-10-25).
func TestRingLEDForTheDot3(t *testing.T) {
	for _, c := range []struct {
		angle float64
		want  int
	}{{180, 0}, {270, 3}, {0, 6}, {90, 9}, {195, 0}, {165, 11}, {359, 5}} {
		if got := ringLEDFor(c.angle, 195); got != c.want {
			t.Errorf("%v degrees: LED %d, want %d", c.angle, got, c.want)
		}
	}
}
