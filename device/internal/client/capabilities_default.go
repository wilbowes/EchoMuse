//go:build !crown

package client

import "github.com/wilbowes/EchoMuse/internal/bindings/als"

// capabilities is what biscuit's firmware implements. Tagged "not crown"
// rather than "server": this package is host-tested with no board tag at
// all (ci.yml never passes one to `go test ./internal/...`), so biscuit has
// to stay the default for every build that isn't explicitly another board —
// same fallback-to-biscuit philosophy as internal/bindings/led.
//
// "ambient_light" is conditional on the hardware actually having a readable
// sensor — the controller advertises an HA entity off the back of it, and an
// entity that can never produce a reading is worse than no entity at all.
func capabilities() []string {
	// "audio_mix": this firmware holds music on its own plane and mixes it
	// with voice at the ALSA write, so the controller can duck instead of
	// pausing. Without it the controller must keep the pause/resume path —
	// a device that cannot mix would simply never play the 0x04 stream.
	//
	// "oww_trigger": this firmware can act on its own wake detection, not
	// just report it. It is separate from "oww_shadow" on purpose — shadow
	// shipped first and there are devices in the field announcing it that
	// cannot trigger, so offering them owwOnDevice="on" would produce a
	// device that scores, stays silent, and looks broken. Announcing a
	// capability the firmware has, rather than inferring one from a version
	// string, is the rule the whole registration follows.
	caps := []string{"mic", "speaker", "leds", "led_anim", "buttons",
		"oww_shadow", "oww_trigger", "button_hold", "audio_mix"}
	if als.Present() {
		caps = append(caps, "ambient_light")
	}
	return caps
}

// modelName is decorative, never branched on — a human-readable label for the
// physical board, never branched on. Kept beside capabilities() since it's
// the same per-board-file split for the same reason (host tests build with
// no tag at all, so biscuit has to stay the default here too).
func modelName() string { return "Echo Dot Gen 2 (biscuit)" }
