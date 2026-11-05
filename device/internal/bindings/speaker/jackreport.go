package speaker

import (
	"github.com/wilbowes/EchoMuse/internal/bindings/codec"
)

// The jack read-back: what the controller can see about the plug and the codec
// state that follows from it.
//
// It exists because the device KNOWS all of this and told nobody. The plug
// position is polled every second (jack.Watch), the routing is reconciled
// against Android's audio HAL every 30s (ReconcileJackRouting), and both were
// visible only in the device's own log — which a support bundle does not
// collect. So every audio report began with a round of questions, and #566's
// bundle could say the device ran emOS on v2.15.0 and not one thing about the
// jack (#621).
//
// ABSENCE IS NULL THROUGHOUT. A device with no accdet switch must not read as
// "nothing plugged in", and a control that could not be read must not read as
// a value: the gain genuinely sits at 0 at the floor of its range, which is
// one of the faults this state exists to expose.

// JackReport is the `jack` object on the periodic stats report.
type JackReport struct {
	// Inserted is whether something is in the 3.5mm jack. nil where the board
	// exposes no detect switch at all.
	Inserted *bool `json:"inserted"`
	// Controls maps each control name to the value AS READ BACK, not as
	// written. The difference is the whole point: Android's audio HAL rewrites
	// the codec underneath us, so the value this firmware last wrote is
	// routinely not the value in force (measured 2026-09-03, every 60-90s on a
	// device with a plug in). Keys are the board's own names plus the two DAC
	// playback routes; a control whose read failed is absent, never "".
	Controls map[string]string `json:"controls,omitempty"`
	// DriftReapplies is how many routing controls this firmware has had to
	// rewrite since the process started, because something else moved them. A
	// climbing count means another writer is winning.
	DriftReapplies int64 `json:"driftReapplies"`
}

// jackReadBack is every control the report carries: the board's jack table,
// plus the two playback DAC routes. The routes are not in the reconcile's
// table on purpose — a route left open is silence rather than an error, and
// writing to it every 30s would paper over a fault instead of reporting it.
func jackReadBack(insert, remove []string) []string {
	var names []string
	seen := map[string]bool{}
	for _, c := range append(append([]string{}, insert...), remove...) {
		if !seen[c] {
			seen[c] = true
			names = append(names, c)
		}
	}
	for _, c := range []string{codec.HPLDacSwitch, codec.HPRDacSwitch} {
		if !seen[c] {
			seen[c] = true
			names = append(names, c)
		}
	}
	return names
}

// jackReport assembles the report from what was actually read.
//
// `detect` is jack.State()'s raw accdet value and `detectOK` whether it could
// be read at all; `current` maps control name to the value read back, with a
// control simply absent where the read failed. Same "failure to look is not
// evidence of absence" rule jackRoutingDrift applies before rewriting.
func jackReport(detect int, detectOK bool, current map[string]string, reapplies int64) *JackReport {
	r := &JackReport{DriftReapplies: reapplies}
	if detectOK {
		inserted := detect != 0
		r.Inserted = &inserted
	}
	if len(current) > 0 {
		r.Controls = make(map[string]string, len(current))
		for k, v := range current {
			r.Controls[k] = v
		}
	}
	return r
}
