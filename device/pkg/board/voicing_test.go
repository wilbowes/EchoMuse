package board

import (
	"math"
	"testing"
)

func TestVolumeEQGainAt(t *testing.T) {
	v := VolumeEQ{Gains: []VolumeGain{{50, 6}, {70, 2}, {90, -4}}}
	for _, c := range []struct{ pct, want float64 }{
		{0, 6}, {50, 6}, {60, 4}, {70, 2}, {80, -1}, {90, -4}, {100, -4},
	} {
		if got := v.GainAt(c.pct); math.Abs(got-c.want) > 1e-9 {
			t.Errorf("GainAt(%g) = %g, want %g", c.pct, got, c.want)
		}
	}
	if got := (VolumeEQ{}).GainAt(40); got != 0 {
		t.Errorf("empty table = %g, want 0", got)
	}
}

// Stock radar adds bass as the volume falls and takes it away at the top;
// the table must keep that direction or the driver gets more bass when it
// can least take it.
func TestRadarBassFollowsVolumeDown(t *testing.T) {
	bass := radarVoicing.ByVolume[0]
	prev := math.Inf(1)
	for pct := 0.0; pct <= 100; pct += 5 {
		g := bass.GainAt(pct)
		if g > prev {
			t.Fatalf("bass rises with volume at %g%%: %g after %g", pct, g, prev)
		}
		prev = g
	}
	if bass.GainAt(100) >= 0 {
		t.Errorf("full volume bass %g, want a cut", bass.GainAt(100))
	}
	for _, st := range radarVoicing.ByVolume {
		for i := 1; i < len(st.Gains); i++ {
			if st.Gains[i].Percent <= st.Gains[i-1].Percent {
				t.Errorf("%gHz: percents not ascending at %d", st.Hz, i)
			}
		}
	}
	if biscuitVoicing.ByVolume != nil {
		t.Error("biscuit has no volume EQ; adding one changes its heard voicing")
	}
}
