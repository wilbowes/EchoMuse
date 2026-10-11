package config

import "testing"

func TestResponseLevelDefaultsLowAndMapsToGain(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{})
	if d.ResponseLevel != ResponseLevelLow || d.ResponseGainDB() != 0 {
		t.Fatalf("default = %q/%gdB, want low/0dB", d.ResponseLevel, d.ResponseGainDB())
	}

	d.Apply(ConfigMessage{ResponseLevel: "medium"})
	if d.ResponseLevel != ResponseLevelMedium || d.ResponseGainDB() != 6 {
		t.Fatalf("medium = %q/%gdB", d.ResponseLevel, d.ResponseGainDB())
	}

	d.Apply(ConfigMessage{ResponseLevel: "HIGH"})
	if d.ResponseLevel != ResponseLevelHigh || d.ResponseGainDB() != 12 {
		t.Fatalf("high = %q/%gdB", d.ResponseLevel, d.ResponseGainDB())
	}
}

func TestUnknownResponseLevelFailsSafeToLow(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{ResponseLevel: "maximum"})
	if d.ResponseLevel != ResponseLevelLow || d.ResponseGainDB() != 0 {
		t.Fatalf("unknown = %q/%gdB, want low/0dB", d.ResponseLevel, d.ResponseGainDB())
	}
}

func TestResponseLevelSurvivesSnapshot(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{ResponseLevel: "high"})
	if got := d.Snapshot().ResponseLevel; got != ResponseLevelHigh {
		t.Fatalf("snapshot responseLevel = %q", got)
	}
}
