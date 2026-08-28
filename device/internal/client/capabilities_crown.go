//go:build crown

package client

// capabilities is crown's MVP subset (docs/device-controller-interface.md,
// "Board profile — crown"). "leds" stays on despite there being no ring —
// the listening-ring hint still drives the wake cue, and as of `229c12c` it
// really does have something to paint with: `StatusOverlay` renders it as a
// glowing status strip along the screen's top edge. "display" is announced
// alongside it because the dashboard needs to know this device has a screen
// to draw a screen-bodied icon, and the capability is what it should check,
// never the decorative model string. No "led_anim": there is no ring for a
// local animation engine to spin frames for. No oww_shadow/oww_trigger: MVP
// wake word is controller-side. No button_hold / ambient_light yet — not
// implemented, not announced (the rule throughout: never sniff the model to
// decide behaviour, only ever add or omit the capability itself). "audio_mix"
// IS announced: pcm_speaker_crown.go holds
// the same two-plane PumpMusic/SetDuck mixer as biscuit, wired through the
// same untagged 0x04/0x05 handling in data.go — the only thing crown-specific
// about playback is that it now goes out over the socket+AudioTrack path
// (2026-08-26 freeze fix) rather than raw ALSA. Ducking under that path has
// since been confirmed on real hardware (2026-08-27) — music audibly ducks
// under a voice turn, verified live — so a duck that sounds wrong here is a
// tuning bug, not a "does crown support this at all" question.
func capabilities() []string {
	return []string{"mic", "speaker", "leds", "buttons", "display", "audio_mix"}
}

// modelName is decorative — display only, never branched on. "crown" is the LineageOS port's own
// codename (ro.product.device=crown) — see the codename note at the top of
// docs/echo-show-8-hardware-map.md: an earlier draft used "hoya", which is
// Amazon's codename for the Echo Show *5*, and the wrong board entirely.
func modelName() string { return "Echo Show 8 Gen 1 (crown)" }
