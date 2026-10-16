package outchain

import "testing"

// Pins the measured values against silent drift — same reason
// controller/tests/test_mbc.py pins em_mbc.CROSSOVER_HZ etc. Change these
// only with a measurement to back it up.
func TestTuningForSelectsByBoard(t *testing.T) {
	cases := []struct {
		boardID             string
		wantCrossoverHz     float64
		wantBassThresholdDb float64
	}{
		{"biscuit", crossoverHz, bassThresholdDb},
		{"radar", radarCrossoverHz, radarBassThresholdDb},
		{"", crossoverHz, bassThresholdDb},     // no board identified
		{"dot3", crossoverHz, bassThresholdDb}, // unrecognised -> biscuit
	}
	for _, c := range cases {
		got := tuningFor(c.boardID)
		if got.crossoverHz != c.wantCrossoverHz || got.bassThresholdDb != c.wantBassThresholdDb {
			t.Errorf("tuningFor(%q) = %+v, want {%g %g}",
				c.boardID, got, c.wantCrossoverHz, c.wantBassThresholdDb)
		}
	}
}

// Radar's own numbers, measured off its MBCL.cfg ("Radar Tuning V4.5") —
// not a copy of biscuit's. See controller/em_mbc.py's equivalent pin.
func TestRadarTuningMatchesItsOwnMeasuredConfiguration(t *testing.T) {
	if radarCrossoverHz != 70.0 {
		t.Errorf("radarCrossoverHz = %g, want 70.0", radarCrossoverHz)
	}
	if radarBassThresholdDb != -25.0 {
		t.Errorf("radarBassThresholdDb = %g, want -25.0", radarBassThresholdDb)
	}
}

// NewForBoard must actually reach the filter design, not just the lookup
// table above — this is what pcm_speaker.go calls in production. Radar
// gets a *radarMultiband now, not a *bassGuard (see newRadarMultiband) —
// its band 1 compressor's threshold must still agree with
// radarBassThresholdDb, the same measured value read twice.
func TestNewForBoardAppliesTheBoardsTuning(t *testing.T) {
	biscuit := NewForBoard(48000, "biscuit", nil)
	bg, ok := biscuit.guard.(*bassGuard)
	if !ok {
		t.Fatalf("biscuit's guard = %T, want *bassGuard", biscuit.guard)
	}
	if bg.thresholdDb != bassThresholdDb {
		t.Errorf("guard.thresholdDb = %g, want %g", bg.thresholdDb, bassThresholdDb)
	}

	rt := radarTuningForTest(t)
	radar := NewForBoard(48000, "radar", rt)
	mb, ok := radar.guard.(*radarMultiband)
	if !ok {
		t.Fatalf("radar's guard = %T, want *radarMultiband", radar.guard)
	}
	if want := rt.MBCL.Bands[0].CompThresh; mb.comp[0].thresholdDb != want {
		t.Errorf("guard.comp[0].thresholdDb = %g, want %g", mb.comp[0].thresholdDb, want)
	}
}
