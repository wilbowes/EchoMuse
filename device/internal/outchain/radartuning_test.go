package outchain

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// radarTuningForTest is a Radar's stock tuning, from the directory named by
// ECHOMUSE_RADAR_TUNING: a copy of an Echo 2's
// /system/vendor/etc/audio-algorithms. The files are not in this repository,
// so without it the test is skipped.
func radarTuningForTest(t testing.TB) *RadarTuning {
	t.Helper()
	dir := os.Getenv("ECHOMUSE_RADAR_TUNING")
	if dir == "" {
		t.Skip("ECHOMUSE_RADAR_TUNING not set: needs a copy of a Radar's audio-algorithms")
	}
	rt, err := LoadRadarTuning(dir)
	if err != nil {
		t.Fatal(err)
	}
	return rt
}

func TestLoadRadarTuningReadsWhatAFEListsAndNothingElse(t *testing.T) {
	rt, err := LoadRadarTuning(filepath.Join("testdata", "radar_tuning"))
	if err != nil {
		t.Fatal(err)
	}
	wantFIR := [][]float64{{1, -0.25, 0.125, 0}, {0.5, 0.25, 0, 0}}
	if !reflect.DeepEqual(rt.FIRBands, wantFIR) || !reflect.DeepEqual(rt.FIRBounds, []float64{40, 100}) {
		t.Errorf("fir = %v at %v", rt.FIRBands, rt.FIRBounds)
	}
	wantPEQ := []tuningBiquad{{"LOW_SHELF", 120, 0.7, 3}, {"PEAK", 90, 1.2, 1}}
	if !reflect.DeepEqual(rt.PEQ, wantPEQ) {
		t.Errorf("peq = %+v, want the two that are not BYPASS", rt.PEQ)
	}
	if rt.TrimDb != 1.5 {
		t.Errorf("trim = %g", rt.TrimDb)
	}
	m := rt.MBCL
	if m == nil || m.InVol != 2 || !reflect.DeepEqual(m.FC, []float64{80, 300, 4000}) ||
		m.Bands[2] != (mbclBand{2, 2, -12, -30, 1, -5, 50}) || m.FullBand != (mbclLimiterSpec{0, -2, 30}) {
		t.Errorf("mbcl = %+v", m)
	}

	// The chain takes all of it: the MBCL in place of the guard and the
	// limiter, the curve available to StockCurve.
	c := NewForBoard(48000, "radar", rt)
	if _, ok := c.guard.(*radarMultiband); !ok {
		t.Errorf("guard = %T", c.guard)
	}
	if lim, ok := c.lim.(*stockLimiter); !ok || lim.thresholdDb != -2 {
		t.Errorf("limiter = %#v", c.lim)
	}
	if len(c.firBands) != 2 || len(c.peq) != 2 || c.trimGain != dbToGain(1.5) {
		t.Errorf("stock curve: %d bands, %d biquads, trim %g", len(c.firBands), len(c.peq), c.trimGain)
	}
}

// A stage AFE.cfg does not list is not loaded, even with its file present.
func TestLoadRadarTuningSkipsUnlistedStages(t *testing.T) {
	dir := t.TempDir()
	for _, f := range []string{"EQ_quiet.cfg", "EQ_loud.cfg", "PEQ.cfg", "MBCL.cfg"} {
		copyFile(t, filepath.Join("testdata", "radar_tuning", f), filepath.Join(dir, f))
	}
	afe := `{"Path Definition": {"Playback": {"Algorithms": {"MBCL": "MBCL"}}},
	         "Algorithm Definition": {"MBCL": {"External Config": ["MBCL.cfg"]}}}`
	if err := os.WriteFile(filepath.Join(dir, "AFE.cfg"), []byte(afe), 0o644); err != nil {
		t.Fatal(err)
	}
	rt, err := LoadRadarTuning(dir)
	if err != nil {
		t.Fatal(err)
	}
	if rt.MBCL == nil || rt.FIRBands != nil || rt.PEQ != nil || rt.TrimDb != 0 {
		t.Errorf("got %s, want the MBCL only", rt)
	}
}

// A listed stage whose file is missing fails the whole load: half the
// tuning is a different sound from the one the files describe.
func TestLoadRadarTuningFailsOnAMissingFile(t *testing.T) {
	dir := t.TempDir()
	for _, f := range []string{"AFE.cfg", "EQ_quiet.cfg", "PEQ.cfg", "MBCL.cfg"} {
		copyFile(t, filepath.Join("testdata", "radar_tuning", f), filepath.Join(dir, f))
	}
	if rt, err := LoadRadarTuning(dir); err == nil {
		t.Fatalf("loaded %s without EQ_loud.cfg", rt)
	}
}

// Without a tuning a Radar chain still takes the volume, and has no stock
// curve, multiband or stock limiter to run.
func TestRadarChainWithoutTuning(t *testing.T) {
	c := NewForBoard(48000, "radar", nil)
	if !c.TakesVolume() || c.firBands != nil {
		t.Error("radar chain without a tuning")
	}
	if _, ok := c.guard.(*bassGuard); !ok {
		t.Errorf("guard = %T, want the single-band guard", c.guard)
	}
}

func copyFile(t *testing.T, from, to string) {
	t.Helper()
	data, err := os.ReadFile(from)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(to, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

// With a plug in (Params.Jack) a Radar chain leaves out the stock curve and
// the multiband, which are tuned for its speaker, and takes them back when
// the plug comes out.
func TestRadarJackLeavesOutTheSpeakerStages(t *testing.T) {
	rt, err := LoadRadarTuning(filepath.Join("testdata", "radar_tuning"))
	if err != nil {
		t.Fatal(err)
	}
	c := NewForBoard(48000, "radar", rt)
	c.SetActive(true)
	c.SetVolumeGain(1)
	p := DefaultParams()
	p.Jack = true
	c.SetParams(p)
	c.Process(loud(2048))
	if c.fir != nil || !c.skipGuard {
		t.Fatalf("jack: fir %v, guard skipped %v", c.fir != nil, c.skipGuard)
	}
	if r := c.guard.takeMaxReductionDb(); r != 0 {
		t.Errorf("jack: the multiband ran (%gdB)", r)
	}

	p.Jack = false
	c.SetParams(p)
	c.Process(loud(2048))
	if c.fir == nil || c.skipGuard {
		t.Fatalf("speaker: fir %v, guard skipped %v", c.fir != nil, c.skipGuard)
	}
}

// The log line names what a Radar chain actually runs: MBCL's limiter, not
// the one in Params, and whether the stock curve is in.
func TestDescribeNamesWhatRadarRuns(t *testing.T) {
	rt, err := LoadRadarTuning(filepath.Join("testdata", "radar_tuning"))
	if err != nil {
		t.Fatal(err)
	}
	p := DefaultParams()
	if got, want := NewForBoard(48000, "radar", rt).Describe(p),
		"eq=flat speech_boost=off guard=-30dB limiter=-2dB/30ms stock_curve=on"; got != want {
		t.Errorf("speaker: got %q, want %q", got, want)
	}
	p.GuardEnabled, p.Jack = false, true
	if got, want := NewForBoard(48000, "radar", rt).Describe(p),
		"eq=flat speech_boost=off guard=off limiter=-2dB/30ms stock_curve=off jack"; got != want {
		t.Errorf("jack: got %q, want %q", got, want)
	}
	if got, want := New(48000).Describe(DefaultParams()), DefaultParams().String(); got != want {
		t.Errorf("biscuit: got %q, want %q", got, want)
	}
}
