package speaker

import (
	"strconv"

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
// ABSENCE IS NULL THROUGHOUT, and every field here is a pointer or omitted for
// the same reason: a device with no accdet switch must not read as "nothing
// plugged in", and HP Driver Gain Volume genuinely sits at 0 — the FLOOR of
// its range, which is precisely the fault this state was added to expose — so
// a plain 0 would be indistinguishable from a control nobody could read.
//
// The switch values are strings and are omitted rather than nulled when the
// read fails, because "On"/"Off"/"1" is a closed value space: the empty
// string is not a reading tinymix can produce, so absence is unambiguous
// without a pointer per field.

// JackReport is the `jack` object on the periodic stats report.
type JackReport struct {
	// Inserted is whether something is in the 3.5mm jack. nil where the board
	// exposes no detect switch at all.
	Inserted *bool `json:"inserted"`
	// SpeakerAmp and DriverGain are Ext_Speaker_Amp_Switch and
	// HP Driver Gain Volume AS READ BACK, not as written. The difference is
	// the whole point: Android's audio HAL rewrites the codec underneath us,
	// so the value this firmware last wrote is routinely not the value in
	// force (measured 2026-09-03, every 60-90s on a device with a plug in).
	SpeakerAmp string `json:"speakerAmp,omitempty"`
	DriverGain *int   `json:"driverGain"`
	// DacL / DacR are the two playback DAPM routes, read back for the same
	// reason: a route left open is silence, not an error.
	DacL string `json:"dacL,omitempty"`
	DacR string `json:"dacR,omitempty"`
	// DriftReapplies is how many routing controls this firmware has had to
	// rewrite since the process started, because something else moved them. A
	// climbing count means another writer is winning.
	DriftReapplies int64 `json:"driftReapplies"`
}

// jackControls are the controls JackReport carries, and jackRoutingDrift's own
// read. Read-back by name through the same mixer_get_ctl_by_name lookup as the
// writes (#546), so a control this board lacks is a failed read — reported as
// absent rather than guessed at from its position.
var jackControls = []string{ctlSpeakerAmp, ctlHPDriverGain}

// jackReadBack is jackControls plus the two DAC routes. Kept as a separate
// list because the RECONCILE only ever rewrites the first pair: a playback
// route going open is a fault to report, not one to paper over by writing to
// it every 30s.
var jackReadBack = []string{
	ctlSpeakerAmp, ctlHPDriverGain, codec.HPLDacSwitch, codec.HPRDacSwitch,
}

// jackReport assembles the report from what was actually read.
//
// `detect` is jack.State()'s raw accdet value and `detectOK` whether it could
// be read at all; `current` maps control name to the value read back, with a
// control simply absent where the read failed. Same "failure to look is not
// evidence of absence" rule jackRoutingDrift applies before rewriting, and
// for the same reason: a failed read must not be published as a value.
func jackReport(detect int, detectOK bool, current map[string]string, reapplies int64) *JackReport {
	r := &JackReport{DriftReapplies: reapplies}
	if detectOK {
		inserted := detect != 0
		r.Inserted = &inserted
	}
	r.SpeakerAmp = current[ctlSpeakerAmp]
	r.DacL = current[codec.HPLDacSwitch]
	r.DacR = current[codec.HPRDacSwitch]
	if v, ok := current[ctlHPDriverGain]; ok {
		// A gain that will not parse is a control this build does not
		// understand, which is absence rather than a level of zero.
		if n, err := strconv.Atoi(v); err == nil {
			r.DriverGain = &n
		}
	}
	return r
}
