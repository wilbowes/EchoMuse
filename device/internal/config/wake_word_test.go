package config

import (
	"encoding/json"
	"testing"
)

// #286: Home Assistant's "No wake word" arrives as wakeWordEnabled=false.
// A device boots with it on, and only the controller turns it off.
func TestWakeWordOnByDefault(t *testing.T) {
	d := &Device{}
	d.loadDefaults()
	if !d.WakeWordOn() {
		t.Fatal("a device that has had no push must answer its wake word")
	}
}

// false is the value that matters, so it must survive the JSON the
// controller sends. A plain bool with omitempty would drop it.
func TestWakeWordOffReachesTheDevice(t *testing.T) {
	d := &Device{}
	d.loadDefaults()

	var msg ConfigMessage
	if err := json.Unmarshal([]byte(`{"type":"config","wakeWordEnabled":false}`), &msg); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	d.Apply(msg)
	if d.WakeWordOn() {
		t.Fatal("wakeWordEnabled=false was ignored")
	}
	if got := d.Snapshot().WakeWordEnabled; got == nil || *got {
		t.Fatalf("snapshot reports %v, want false", got)
	}

	// A sparse push leaves it where it is.
	d.Apply(ConfigMessage{Type: "config", OwwThreshold: 0.6})
	if d.WakeWordOn() {
		t.Fatal("a push without wakeWordEnabled turned the wake word back on")
	}

	on := true
	d.Apply(ConfigMessage{Type: "config", WakeWordEnabled: &on})
	if !d.WakeWordOn() {
		t.Fatal("wakeWordEnabled=true did not turn it back on")
	}
}
