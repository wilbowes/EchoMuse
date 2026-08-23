package server

import (
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

func litIndices(f []led.Led) map[int]bool {
	out := map[int]bool{}
	for _, l := range f {
		if l.B > 0 || l.R > 0 || l.G > 0 {
			out[l.ID] = true
		}
	}
	return out
}

// #316: the sweep is mirrored — heads at pos and 12-pos, so the pattern is
// symmetric around the front axis and reads as deliberate rather than as a
// glitch.
func TestCylonFrameIsMirrored(t *testing.T) {
	for pos := 0; pos <= 6; pos++ {
		lit := litIndices(CylonFrame(pos, +1))
		for i := range lit {
			mirror := (12 - i) % 12
			if !lit[mirror] {
				t.Fatalf("pos %d: LED %d lit but mirror %d dark — not symmetric", pos, i, mirror)
			}
		}
	}
}

func TestCylonHeadsOutshineTheTrail(t *testing.T) {
	f := CylonFrame(3, +1)
	var head, trail led.Led
	for _, l := range f {
		if l.ID == 3 {
			head = l
		}
		if l.ID == 2 {
			trail = l
		}
	}
	if head.B <= trail.B {
		t.Fatalf("head (%d) must outshine trail (%d) to read as a comet", head.B, trail.B)
	}
}

// The colour must stay clear of the four taken states: red (mute), orange
// (link down), cyan (volume arc), white (pending approval). Blue-violet:
// red low relative to blue, green never dominating.
func TestCylonColourAvoidsTheTakenStates(t *testing.T) {
	for _, d := range []int{+1, -1} {
		for pos := 0; pos <= 6; pos++ {
			for _, l := range CylonFrame(pos, d) {
				if l.B == 0 && l.R == 0 && l.G == 0 {
					continue
				}
				if l.R > l.B/2 {
					t.Fatalf("red channel too high — collides with mute red or link orange")
				}
				if l.G > l.B {
					t.Fatalf("green dominates — drifts toward cyan (volume arc)")
				}
			}
		}
	}
}

func TestCylonTrailFoldsAtTheTurningPoints(t *testing.T) {
	// Top turn (pos=0 heading up): head on 0, trails on 1/11.
	lit := litIndices(CylonFrame(0, +1))
	want := map[int]bool{0: true, 1: true, 11: true}
	for i := range lit {
		if !want[i] {
			t.Fatalf("pos=0: unexpected LED %d lit", i)
		}
	}
	// Bottom turn (pos=6 heading down): comets merged on 6, trails spread
	// back up both sides (5/7's mirror 11).
	lit = litIndices(CylonFrame(6, -1))
	want2 := map[int]bool{5: true, 6: true, 7: true}
	for i := range lit {
		if !want2[i] {
			t.Fatalf("pos=6: unexpected LED %d lit", i)
		}
	}
}
