package speaker

import "github.com/wilbowes/EchoMuse/internal/bindings/mixer"

// Jack routing: the codec state each plug position needs.
//
// Here rather than in pcm_speaker.go because that file is ARM-only (build tag
// `server`) and the mapping is worth pinning on the host — it is a table of
// measured values, and a typo in one of them is silence rather than an error.
//
// MEASURED 2026-09-03 against a stock FireOS 5.5.5.4 Dot (root, no EchoMuse)
// driving the same cable, by diffing all 239 mixer controls across an insert
// on both devices. Stock changed five controls; we changed one.

// The two controls, by name: the internal driver's amp and the jack's output
// stage.
const (
	ctlSpeakerAmp   = mixer.SpeakerAmp
	ctlHPDriverGain = mixer.HPDriverGain
)

// HP driver gain values, as mixer indices on a 0..35 range that maps to
// -6dB..+29dB.
//
// hpGainInternal is what both a stock Dot and ours sit at with nothing
// plugged in. hpGainJack is stock's value with a plug in.
//
// The gap is the whole bug. Something drops this control to 0 — the FLOOR of
// the range, -6dB — when a plug goes in, and on a stock device the audio HAL
// then raises it to 11. We had nothing that did, so the external output sat at
// minimum gain: measured inaudible at 0, and audible immediately on writing
// 11 with music playing. It reads as "the jack does not work" rather than as
// "the jack is quiet", which is why it survived so long.
//
// NOT accdet, despite what this project's docs said for a month. Amazon's
// accdet driver (accdet_amzn.c) only reads a GPIO and calls switch_set_state
// on /sys/class/switch/h2w — it writes no mixer control and touches no codec
// register. Everything attributed to it is Android's audio HAL reacting to
// that switch state, which is also why it keeps happening: see
// reconcileJackRouting.
const (
	hpGainInternal = "6"
	hpGainJack     = "11"
)

// mixerWrite sets one control, by name.
type mixerWrite struct {
	Ctl  string
	Args []string
}

// jackRouting returns the mixer writes that put the codec into the state a
// given plug position needs.
//
// Two controls, deliberately. Stock also clears Right Channel Only and sets
// Ignore Ramp Up on insert, and NEITHER is copied here:
//
//   - Right Channel Only selects which codec channel carries the signal, and
//     our wire is mono — toStereo duplicates L into R — so both channels carry
//     the same samples whichever way it is set. It becomes real the day the
//     wire carries stereo, and not before.
//   - Ignore Ramp Up has not been measured on this hardware at all. Copying a
//     stock value whose effect is unknown is how you ship a change that cannot
//     be defended when it turns out to do something else.
//
// The order within a position does not matter: these are independent controls
// on a codec that is already clocking, not a sequence.
func jackRouting(inserted bool) []mixerWrite {
	if inserted {
		// Something is in the jack, so the internal driver must be silent —
		// otherwise the Dot plays to the room while the cable carries the
		// same audio somewhere else.
		return []mixerWrite{
			{Ctl: ctlSpeakerAmp, Args: []string{"Off"}},
			{Ctl: ctlHPDriverGain, Args: []string{hpGainJack, hpGainJack}},
		}
	}
	// Nothing in the jack: the internal driver is the only output there is.
	return []mixerWrite{
		{Ctl: ctlSpeakerAmp, Args: []string{"On"}},
		{Ctl: ctlHPDriverGain, Args: []string{hpGainInternal, hpGainInternal}},
	}
}

// ── Drift ────────────────────────────────────────────────────────────────────
//
// Applying the routing once on a jack edge is not enough, and this is measured
// rather than anticipated. Android's audio HAL rewrites the codec whenever
// mediaserver restarts, which on a device holding pcm23p with a plug inserted
// is roughly every 60-90 seconds. Observed directly on 2026-09-03: HP Driver
// Gain set to 11 came back as 0 within a minute, every minute, and each revert
// coincided exactly with mediaserver taking a new pid. So the jack works for
// about a minute after a plug event and then goes quiet again.
//
// We cannot stop it — mediaserver publishes AudioFlinger and AudioPolicyService
// and system_server crash-loops without them, taking WiFi down with it (tested,
// 2026-09-03). So the routing is reconciled instead: read the controls back,
// and rewrite only the ones that moved.

// jackRoutingDrift returns the writes needed to bring the codec back to the
// state `inserted` requires, given what the controls currently read.
//
// `current` maps control name to the value read back. A control MISSING from
// the map is skipped rather than rewritten: an unreadable control means the
// read failed, and "failure to look is not evidence of absence" applies here
// exactly as it does to the controller's asset reconcile — rewriting on a
// failed read would rewrite it every interval forever.
func jackRoutingDrift(inserted bool, current map[string]string) []mixerWrite {
	var out []mixerWrite
	for _, w := range jackRouting(inserted) {
		got, ok := current[w.Ctl]
		if !ok {
			continue
		}
		if got != w.Args[0] {
			out = append(out, w)
		}
	}
	return out
}
