package speaker

import (
	"strconv"
	"strings"
)

// Jack routing: the codec state each plug position needs.
//
// Here rather than in pcm_speaker.go because that file is ARM-only (build tag
// `server`) and the mapping is worth pinning on the host — it is a table of
// measured values, and a typo in one of them is silence rather than an error.
//
// MEASURED 2026-09-03 against a stock FireOS 5.5.5.4 Dot (root, no EchoMuse)
// driving the same cable, by diffing all 239 mixer controls across an insert
// on both devices. Stock changed five controls; we changed one.

// Mixer control ids on this board. Named because "62" at a call site is the
// difference between the internal driver and the jack, and nothing about the
// number says so.
const (
	ctlSpeakerAmp   = "5"  // Ext_Speaker_Amp_Switch — internal driver's amp
	ctlHPDriverGain = "62" // HP Driver Gain Volume — the jack's output stage
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

// mixerWrite is one `tinymix -D 0 <ctl> <args...>` invocation.
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

// tinymixValue extracts the CURRENT value from one `tinymix -D 0 <ctl>` line.
//
// Two shapes, because the tool prints enums and integers differently:
//
//	Ext_Speaker_Amp_Switch:  >Off  On          → enum, ">" marks current
//	HP Driver Gain Volume: 11 11 (range 0->35) → int, first field after ":"
//
// The enum form is the awkward one: the current value is marked in place
// rather than printed on its own, so a naive "first token after the colon"
// reads the wrong entry whenever the current value is not the first option.
// Returns ok=false for anything unrecognised — a control we cannot READ is not
// a control we should assume has drifted, since that would rewrite it forever.
func tinymixValue(out string) (string, bool) {
	i := strings.Index(out, ":")
	if i < 0 {
		return "", false
	}
	fields := strings.Fields(out[i+1:])
	for _, f := range fields {
		if strings.HasPrefix(f, ">") {
			return strings.TrimPrefix(f, ">"), true // enum
		}
	}
	if len(fields) > 0 && fields[0] != "" {
		// Integer form. Only the first channel is compared: both are always
		// written to the same value, so a difference between them would mean
		// something outside this file is writing one channel on its own.
		if _, err := strconv.Atoi(fields[0]); err == nil {
			return fields[0], true
		}
	}
	return "", false
}

// jackRoutingDrift returns the writes needed to bring the codec back to the
// state `inserted` requires, given what the controls currently read.
//
// `current` maps control id to the value read back. A control MISSING from the
// map is skipped rather than rewritten: an unreadable control means the read
// failed, and "failure to look is not evidence of absence" applies here exactly
// as it does to the controller's asset reconcile — rewriting on a failed read
// would spawn two tinymix processes every interval forever on any device whose
// output we cannot parse.
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
