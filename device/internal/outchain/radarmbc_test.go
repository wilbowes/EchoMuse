package outchain

import (
	"math"
	"testing"
)

// Stock's limiter never lets a sample past its threshold, and its release
// is clamped to 180..400ms whatever the config asks (libasp 0x8d75c).
func TestStockLimiterPinsAtThresholdAndClampsRelease(t *testing.T) {
	l := newStockLimiter(48000, -6.0, 20.0, 0)
	if l.la != 96 || l.holdN != 20 {
		t.Fatalf("look-ahead %d hold %d, want 96 and 20", l.la, l.holdN)
	}
	if l.relN != 8640 {
		t.Errorf("release %d samples, want 8640 (20ms clamps to 180ms)", l.relN)
	}
	if n := stockReleaseSamples(1000, 48000); n != 19200 {
		t.Errorf("1000ms release = %d samples, want 19200 (400ms)", n)
	}
	var peak float64
	for i := 0; i < 48000; i++ {
		y := l.step(32000 * math.Sin(2*math.Pi*200*float64(i)/48000))
		peak = math.Max(peak, math.Abs(y))
	}
	if want := l.thresh; peak > want*(1+1e-12) {
		t.Errorf("peak out %g over the threshold %g", peak, want)
	}
	if r, _, _ := l.takeStats(); r < 5 {
		t.Errorf("reduction %gdB, want ~6dB on a 0dBFS tone at -6dB", r)
	}
}

// A signal entirely below every band's threshold must pass through at
// unity — the network's crossover alone (no law engaging) still has the
// fixed system gain and bands 3/4's input trims, so "no law" is not the
// same claim as "no change in level" (see the Python RadarMultiband's own
// equivalent test and process's comment on why those trims are
// unconditional).
func TestRadarMultibandQuietSignalEngagesNoLaw(t *testing.T) {
	m := newRadarMultiband(48000, radarTuningForTest(t).MBCL)
	m.enabled = true
	// Stock seeds every band's level at 0.01 (-20dB power), over band 1's
	// -25dB threshold, so a fresh compressor reduces briefly while that
	// level falls and its gain eases back (~654ms). That is stock's own
	// start; the claim is about the steady state after it.
	for i := 0; i < 6*48000; i++ {
		if i == 5*48000 {
			m.takeMaxReductionDb()
		}
		// Very quiet relative to every threshold (-40dB floor, -25..-10dB
		// thresholds): 1000Hz at roughly -60dBFS.
		x := 32.0 * math.Sin(2*math.Pi*1000*float64(i)/48000)
		m.step(x)
	}
	if r := m.takeMaxReductionDb(); r > 0.01 {
		t.Errorf("max reduction = %gdB on a signal well under every threshold, want ~0", r)
	}
}

// A loud low tone must engage band 1's law (compressor + its own limiter)
// hard, mirroring em_mbc.py's own RadarMultiband smoke test.
func TestRadarMultibandLoudBassEngagesBandOne(t *testing.T) {
	m := newRadarMultiband(48000, radarTuningForTest(t).MBCL)
	m.enabled = true
	fs := 48000.0
	var peakOut float64
	for i := 0; i < int(fs*2); i++ {
		x := 30000.0 * math.Sin(2*math.Pi*50*float64(i)/fs)
		y := m.step(x)
		if a := math.Abs(y); a > peakOut {
			peakOut = a
		}
	}
	if r := m.comp[0].takeMaxReductionDb(); r < 10.0 {
		t.Errorf("band 1 compressor max reduction = %gdB, want well over 10dB on a loud 50Hz tone", r)
	}
	if peakOut >= 30000.0 {
		t.Errorf("peak out = %g, want it reduced from the 30000 input", peakOut)
	}
}

// Disabling must freeze every stage's gain state rather than resetting it
// — toggling back on should resume from where it left off, not from
// unity, same contract as bassGuard/limiter bypass.
func TestRadarMultibandDisableFreezesGainState(t *testing.T) {
	m := newRadarMultiband(48000, radarTuningForTest(t).MBCL)
	m.enabled = true
	for i := 0; i < 4800; i++ {
		m.step(30000.0 * math.Sin(2*math.Pi*50*float64(i)/48000))
	}
	gainBefore := m.comp[0].gain
	if gainBefore == 1 {
		t.Fatal("band 1 compressor never engaged — nothing to freeze")
	}
	m.setEnabled(false)
	for i := 0; i < 100; i++ {
		m.step(0)
	}
	if m.comp[0].gain != gainBefore {
		t.Errorf("gain drifted from %g to %g while disabled, want frozen",
			gainBefore, m.comp[0].gain)
	}
}

// setFloorDb must reach band 1 only — bands 2-4 have no dashboard control,
// same reasoning as the limiter override.
func TestRadarMultibandSetFloorDbOnlyTouchesBandOne(t *testing.T) {
	m := newRadarMultiband(48000, radarTuningForTest(t).MBCL)
	want2 := m.comp[1].floorDb
	m.setFloorDb(-10.0)
	if m.comp[0].floorDb != -10.0 {
		t.Errorf("comp[0].floorDb = %g, want -10", m.comp[0].floorDb)
	}
	if m.comp[1].floorDb != want2 {
		t.Errorf("comp[1].floorDb changed to %g, want unchanged %g", m.comp[1].floorDb, want2)
	}
}

// reset must actually clear every one of the twelve filter pairs' state —
// a stale biquad left non-zero would leave an audible tail on the device
// at every reactivation.
func TestRadarMultibandResetClearsEveryFilter(t *testing.T) {
	m := newRadarMultiband(48000, radarTuningForTest(t).MBCL)
	m.enabled = true
	for i := 0; i < 4800; i++ {
		m.step(30000.0 * math.Sin(2*math.Pi*500*float64(i)/48000))
	}
	m.reset()
	pairs := []*[2]biquad{
		&m.lp1, &m.hp1, &m.lp2, &m.hp2, &m.lp2c, &m.hp2c,
		&m.lp3, &m.hp3, &m.lp3c1, &m.hp3c1, &m.lp3c2, &m.hp3c2,
	}
	for pi, p := range pairs {
		for i, bq := range p {
			if bq.z1 != 0 || bq.z2 != 0 {
				t.Errorf("pair %d stage %d not cleared: z1=%g z2=%g", pi, i, bq.z1, bq.z2)
			}
		}
	}
	for i := range m.comp {
		if m.comp[i].gain != 1 || m.comp[i].level != compInit {
			t.Errorf("comp[%d] gain=%g level=%g after reset, want stock's 1 and 0.01", i, m.comp[i].gain, m.comp[i].level)
		}
		if m.lim[i].g != 1 || m.lim[i].hold != 0 || m.lim[i].rel != 0 {
			t.Errorf("lim[%d] g=%g hold=%d rel=%d after reset, want 1/0/0", i, m.lim[i].g, m.lim[i].hold, m.lim[i].rel)
		}
	}
}
