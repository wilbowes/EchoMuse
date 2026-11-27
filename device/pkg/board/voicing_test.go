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

// Radar's button floor (level 57, internal/server volume.go) is at -50dB,
// set by ear on 035T (#1157): -33dB measured ~61dBA at 1m, conversation
// level.
func TestRadarButtonFloor(t *testing.T) {
	v := radarVoicing
	if got := float64(57-127)*v.DbPerLevel + v.CeilingDb; math.Abs(got+50) > 1e-9 {
		t.Errorf("radar floor %v dB, want -50", got)
	}
}

func TestDonutButtonFloor(t *testing.T) {
	v := donutVoicing
	if got := float64(57-127)*v.DbPerLevel + v.CeilingDb; math.Abs(got+50) > 1e-9 {
		t.Errorf("donut floor %v dB, want -50", got)
	}
}

// Stock Dot 3 adds bass as the volume falls and takes it away at the top, as
// radar does; the table must keep that direction.
func TestDonutBassFollowsVolumeDown(t *testing.T) {
	bass := donutVoicing.ByVolume[0]
	prev := math.Inf(1)
	for pct := 0.0; pct <= 100; pct += 5 {
		g := bass.GainAt(pct)
		if g > prev {
			t.Fatalf("bass rises with volume at %g%%: %g after %g", pct, g, prev)
		}
		prev = g
	}
	if bass.GainAt(100) >= 0 || bass.GainAt(40) <= 0 {
		t.Errorf("bass %g at 100%%, %g at 40%%: want a cut at the top, a lift below", bass.GainAt(100), bass.GainAt(40))
	}
	for i := 1; i < len(bass.Gains); i++ {
		if bass.Gains[i].Percent <= bass.Gains[i-1].Percent {
			t.Errorf("percents not ascending at %d", i)
		}
	}
}

// The Dot 3's bass follows loudness, not the knob: at every level in the
// button band it equals #1230's table read at the level that gave the same
// loudness on the plain 0.5dB slope.
func TestDonutBassFollowsLoudness(t *testing.T) {
	plain := VolumeEQ{Gains: []VolumeGain{
		{45, 5.10}, {55, 3.46}, {65, 0}, {75, -2.70}, {85, -8.18}, {95, -8.85},
	}}
	bass := donutVoicing.ByVolume[0]
	for level := 57; level <= 127; level++ {
		db := float64(level-127)*donutVoicing.DbPerLevel + donutVoicing.CeilingDb
		plainLevel := 127 + (db-donutVoicing.CeilingDb)/0.5
		want := plain.GainAt(plainLevel * 100 / 127)
		got := bass.GainAt(float64(level) * 100 / 127)
		if math.Abs(got-want) > 1e-9 {
			t.Errorf("level %d (%.1fdB): bass %.3f, want %.3f", level, db, got, want)
		}
	}
}

// The Dot 3's protection comes from 41C8's own distortion map, never from
// stock's MBCL.cfg, whose always-working sub band pumped in a blind listen
// (2026-11-19). The driver is 24-32dB down at 63-100Hz and its 3rd harmonic
// there is -15dB at -12dBFS: the sub band is a fixed high-pass, and nothing
// compresses where the driver is clean.
func TestDonutProtectionIsFromItsDriver(t *testing.T) {
	v, hw := donutVoicing, donutHardware.Speaker
	if v.HighpassHz < 70 {
		t.Errorf("high-pass %gHz: below 70Hz the driver radiates nothing and distorts most", v.HighpassHz)
	}
	if g := hw.BassGuard.ThresholdDb; g < -30 || g > -18 {
		t.Errorf("bass guard from %gdB: want a backstop near -24dB, where 63Hz distortion climbs; stock's -50 pumped", g)
	}
	if b := v.Upper[0]; b.ThresholdDb > -18 || b.ThresholdDb < -30 {
		t.Errorf("70-500Hz band from %gdB: 100Hz is ~13%% distortion at -18dBFS", b.ThresholdDb)
	}
	for i, b := range v.Upper[1:] {
		if b.ThresholdDb < -12 {
			t.Errorf("upper band %d from %gdB: the mids are linear to -12dBFS", i+2, b.ThresholdDb)
		}
	}
}
