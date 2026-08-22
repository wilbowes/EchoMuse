package config

import "testing"

// The output-chain keys are the reason ConfigMessage grew seven pointer
// fields, and the reason is worth a test rather than a comment: 0.0 is a
// legitimate setting for every EQ band and for the limiter threshold, and
// false is legitimate for both toggles. Under the "non-zero means set" rule
// the rest of the message uses, none of those could be distinguished from an
// absent field — so setting a band to 0dB, or turning the limiter off, would
// silently do nothing.
//
// That is exactly the failure the whole feature exists to remove: a control
// that saves, reports success, and changes no audio.

// devWithChain returns a Device in the state a fielded one is actually in:
// defaults loaded, INITIALISED, then configured.
//
// The initialised flag matters and is not bookkeeping. Apply() calls
// loadDefaults() on its first invocation (config.go:191), so a Device built by
// hand with fields assigned directly has every one of them overwritten by the
// first push — which is what the first version of this helper did, and the
// resulting failure reads exactly like Apply clearing a field it should have
// left alone.
func devWithChain() *Device {
	d := &Device{}
	d.loadDefaults()
	d.initialised = true
	d.EqBands = []float64{1, 2, 3, 4, 5, 6, 7, 8}
	d.EqLoudness = true
	d.LimiterEnabled = true
	d.LimiterThreshold = -1
	d.LimiterRelease = 150
	d.BassGuardEnabled = true
	d.BassGuardDb = -30
	return d
}

func TestZeroAndFalseAreApplied(t *testing.T) {
	zeroBands := make([]float64, 8)
	f := false
	zero := 0.0

	d := devWithChain()
	d.Apply(ConfigMessage{
		EqBands:          zeroBands,
		EqLoudness:       &f,
		LimiterEnabled:   &f,
		LimiterThreshold: &zero,
		BassGuardEnabled: &f,
		BassGuardDb:      &zero,
	})

	for i, v := range d.EqBands {
		if v != 0 {
			t.Errorf("band %d = %v, want 0 — a flat band was treated as absent", i, v)
		}
	}
	if d.EqLoudness {
		t.Error("loudness stayed on after being set false")
	}
	if d.LimiterEnabled {
		t.Error("limiter stayed enabled after being set false")
	}
	if d.LimiterThreshold != 0 {
		t.Errorf("limiter threshold = %v, want 0", d.LimiterThreshold)
	}
	if d.BassGuardEnabled {
		t.Error("bass guard stayed enabled after being set false")
	}
	if d.BassGuardDb != 0 {
		t.Errorf("bass guard depth = %v, want 0", d.BassGuardDb)
	}
}

// A partial push must leave everything it does not mention alone. Config
// arrives partial by design — per-section scoping makes that the normal case —
// so a message carrying one key must not reset the other six to their zero
// values.
func TestPartialPushLeavesTheRestAlone(t *testing.T) {
	d := devWithChain()
	thr := -6.0
	d.Apply(ConfigMessage{LimiterThreshold: &thr})

	if d.LimiterThreshold != -6 {
		t.Errorf("threshold = %v, want -6", d.LimiterThreshold)
	}
	if !d.LimiterEnabled || !d.BassGuardEnabled || !d.EqLoudness {
		t.Error("a partial push cleared toggles it did not mention")
	}
	if d.BassGuardDb != -30 || d.LimiterRelease != 150 {
		t.Error("a partial push reset scalars it did not mention")
	}
	if len(d.EqBands) != 8 || d.EqBands[0] != 1 {
		t.Error("a partial push cleared the EQ curve")
	}
}

// The curve must be COPIED out of the message. Holding the caller's slice
// would let a later decode of another message rewrite the live config from
// under the audio thread.
func TestEqBandsAreCopiedNotAliased(t *testing.T) {
	d := &Device{}
	incoming := []float64{1, 2, 3, 4, 5, 6, 7, 8}
	d.Apply(ConfigMessage{EqBands: incoming})
	incoming[0] = 99
	if d.EqBands[0] == 99 {
		t.Error("stored EQ curve aliases the message's slice")
	}
}

// OutputChain() must copy too, for the same reason — it is read on the audio
// path while Apply may be writing.
func TestOutputChainAccessorCopies(t *testing.T) {
	d := devWithChain()
	got := d.OutputChain()
	got.EqBands[0] = 99
	if d.EqBands[0] == 99 {
		t.Error("OutputChain() returned a reference into the live config")
	}
	if got.LimiterThreshold != -1 || !got.BassGuardEnabled || got.BassGuardDb != -30 {
		t.Errorf("OutputChain() lost values: %+v", got)
	}
}

// The defaults are what plays between boot and the first config push, on a
// device that has already told the controller to stop shaping. They must
// mirror the controller's DEFAULT_DEVICE_CONFIG rather than being zero values.
func TestDefaultsMirrorTheController(t *testing.T) {
	d := &Device{}
	d.loadDefaults()

	if len(d.EqBands) != 8 {
		t.Errorf("default EQ has %d bands, want 8", len(d.EqBands))
	}
	for i, v := range d.EqBands {
		if v != 0 {
			t.Errorf("default band %d = %v, want flat", i, v)
		}
	}
	if !d.LimiterEnabled {
		t.Error("limiter defaults off; the controller defaults it ON, and a " +
			"device that has stood the controller down would then clip")
	}
	if !d.BassGuardEnabled {
		t.Error("bass guard defaults off; the controller defaults it ON")
	}
	if d.LimiterThreshold != -1 {
		t.Errorf("default limiter threshold %v, want -1", d.LimiterThreshold)
	}
	if d.LimiterRelease != 150 {
		t.Errorf("default limiter release %v, want 150", d.LimiterRelease)
	}
	if d.BassGuardDb != -30 {
		t.Errorf("default bass guard depth %v, want -30", d.BassGuardDb)
	}
}
