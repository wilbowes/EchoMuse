package config

import (
	"encoding/json"
	"testing"
)

// #263: the controller pushes the current listening-ring animation spec
// alongside config so the device can light it locally at its own wake
// crossing. It rides ConfigMessage as raw JSON (same shape as the led_anim
// control message) so this package does not depend on the renderer.
func TestListeningAnimRoundTripsThroughConfigMessage(t *testing.T) {
	spec := json.RawMessage(`{"pattern":"solid","colors":[[0,255,0]],"listening":true,"ttlSec":30}`)

	d := Get()
	msg := ConfigMessage{Type: "config", ListeningAnim: spec}
	d.Apply(msg)

	got := d.Snapshot().ListeningAnim
	if len(got) == 0 {
		t.Fatal("a pushed listeningAnim must survive into Snapshot")
	}
	var parsed struct {
		Pattern   string `json:"pattern"`
		Listening bool   `json:"listening"`
	}
	if err := json.Unmarshal(got, &parsed); err != nil {
		t.Fatalf("stored spec is not valid JSON: %v", err)
	}
	if parsed.Pattern != "solid" || !parsed.Listening {
		t.Fatalf("spec mangled in storage: %s", got)
	}
}

// Partial-update semantics: a push WITHOUT listeningAnim must not clear one
// already cached — config messages are sparse, and the animation only
// changes when the controller says so.
func TestListeningAnimSurvivesASparseConfigPush(t *testing.T) {
	d := Get()
	d.Apply(ConfigMessage{Type: "config",
		ListeningAnim: json.RawMessage(`{"pattern":"pulse"}`)})
	d.Apply(ConfigMessage{Type: "config", OwwThreshold: 0.5})

	if len(d.Snapshot().ListeningAnim) == 0 {
		t.Fatal("a push without listeningAnim cleared the cached spec")
	}
}
