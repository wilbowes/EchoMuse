package sendspin

import (
	"path/filepath"
	"testing"
)

// One volume (Wil, 2026-09-30): with the device owning it, a server's volume
// command moves the DEVICE's volume and the synced music itself plays at
// unity, so the level is never applied twice.
func TestServerVolumeMovesTheDeviceVolume(t *testing.T) {
	c, err := New(Config{StorePath: filepath.Join(t.TempDir(), "s.json")})
	if err != nil {
		t.Fatal(err)
	}
	var got []int
	c.OnVolume(func(v int) { got = append(got, v) })

	set := c.store.settings()
	set.Volume = 40
	c.applySettings(set)
	if len(got) != 1 || got[0] != 40 {
		t.Fatalf("device told %v, want [40]", got)
	}
	if c.player.gainTarget != unity {
		t.Errorf("synced music attenuated by the server volume as well: gain %d", c.player.gainTarget)
	}

	// The device echoing its new level back is not a change.
	c.SetVolume(40)
	if c.store.settings().Volume != 40 || len(got) != 1 {
		t.Error("the echo of a server's own command looped")
	}
	// A button press is: it becomes what the server is told.
	c.SetVolume(55)
	if c.store.settings().Volume != 55 {
		t.Error("device volume not recorded for the server")
	}

	// Mute stays the player's own: the Echo's mute button is the mic.
	set = c.store.settings()
	set.Muted = true
	c.applySettings(set)
	if c.player.gainTarget != 0 {
		t.Error("server mute did not silence synced music")
	}
}

// Without the device owning volume (the interop harness), the server volume
// is a gain on synced music, as before.
func TestWithoutADeviceVolumeTheServerVolumeIsAGain(t *testing.T) {
	c, _ := New(Config{StorePath: filepath.Join(t.TempDir(), "s.json")})
	set := c.store.settings()
	set.Volume = 50
	c.applySettings(set)
	if c.player.gainTarget != volumeGain(50, false) {
		t.Errorf("gain %d, want %d", c.player.gainTarget, volumeGain(50, false))
	}
}

// The Echo's scale is 0..127 (unity at 127); HA maps it proportionally and
// so must this, so one volume reads one number everywhere.
func TestVolumeMappingRoundTrips(t *testing.T) {
	for pct := 0; pct <= 100; pct++ {
		if got := LevelToPercent(PercentToLevel(pct, 127), 127); got != pct {
			t.Errorf("%d%% -> %d -> %d%%", pct, PercentToLevel(pct, 127), got)
		}
	}
	if PercentToLevel(100, 127) != 127 || LevelToPercent(127, 127) != 100 || PercentToLevel(0, 127) != 0 {
		t.Error("endpoints")
	}
}
