package speaker

import (
	"github.com/wilbowes/EchoMuse/internal/outchain"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// Jack routing: the codec state each plug position needs, from the board's
// table (pkg/board, where each value's measurement is recorded).
//
// Here rather than in pcm_speaker.go because that file is ARM-only (build tag
// `server`) and the mapping is worth pinning on the host — it is a table of
// measured values, and a typo in one of them is silence rather than an error.

// mixerWrite sets one control, by name.
type mixerWrite struct {
	Ctl  string
	Args []string
}

func writesOf(ctls []board.Ctl) []mixerWrite {
	ws := make([]mixerWrite, len(ctls))
	for i, c := range ctls {
		ws[i] = mixerWrite{Ctl: c.Name, Args: c.Values}
	}
	return ws
}

// jackRouting returns the mixer writes that put the codec into the state a
// given plug position needs, in the board's order: the amp off first on
// insert and on last on removal.
func jackRouting(j board.Jack, inserted bool) []mixerWrite {
	if inserted {
		return writesOf(j.Insert)
	}
	return writesOf(j.Remove)
}

// jackControls is every control the jack table writes, for reading back.
func jackControls(j board.Jack) []string {
	var names []string
	seen := map[string]bool{}
	for _, c := range append(append([]board.Ctl{}, j.Insert...), j.Remove...) {
		if !seen[c.Name] {
			seen[c.Name] = true
			names = append(names, c.Name)
		}
	}
	return names
}

// chainForJack returns the output chain configuration a plug position needs:
// the controller's params unchanged, except that the bass guard is bypassed
// while something is in the jack.
//
// The guard exists for the INTERNAL driver — it pulls down the band below
// 115Hz that the driver cannot radiate (em_mbc.py: -17.7dB at 50Hz, -5dB
// overall). A cable carries that band to an amplifier or headphones that can
// reproduce it, so the guard only throws it away there. "Speaker protection"
// was one switch for both outputs, so a jack user chose between thin line-out
// and an unguarded internal speaker.
//
// Only the guard. The limiter stays as configured — it is what stops the EQ
// hard-clipping what it boosts (#231), and that is as true on a cable as on
// the driver — and the EQ is the user's.
func chainForJack(p outchain.Params, inserted bool) outchain.Params {
	if inserted {
		p.GuardEnabled = false
		// The stock-sound correction is for THIS driver; into headphones or
		// an external speaker its +30dB at 175Hz is wrong, so the old law and
		// order apply while a plug is in.
		p.StockSound = false
	}
	return p
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
func jackRoutingDrift(j board.Jack, inserted bool, current map[string]string) []mixerWrite {
	var out []mixerWrite
	for _, w := range jackRouting(j, inserted) {
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

// stockLawOn is whether the stock-sound volume law and order are in force:
// the board has a voicing, the chain runs on this device, and its params ask
// for the profile with no plug in. The voicing test is its own: the params
// still ask for the profile on a board with none (DefaultParams), since the
// chain clears it only on its own copy, and radar applied the law with a nil
// voicing and panicked on every connect (2026-10-24).
func stockLawOn(v *board.Voicing, chainActive bool, p outchain.Params, plugged bool) bool {
	return v != nil && chainActive && chainForJack(p, plugged).StockSound
}
