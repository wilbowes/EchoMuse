package config

import "testing"

func TestRemoteVolumeArcDefaultsOffAndAppliesBothValues(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{})
	if d.RemoteVolumeArcEnabled() {
		t.Fatal("remote volume arc must default off")
	}

	on := true
	d.Apply(ConfigMessage{RemoteVolumeArc: &on})
	if !d.RemoteVolumeArcEnabled() {
		t.Fatal("true remoteVolumeArc was not applied")
	}

	off := false
	d.Apply(ConfigMessage{RemoteVolumeArc: &off})
	if d.RemoteVolumeArcEnabled() {
		t.Fatal("false remoteVolumeArc was not applied")
	}
	if got := d.Snapshot().RemoteVolumeArc; got == nil || *got {
		t.Fatalf("snapshot did not preserve false: %v", got)
	}
}
