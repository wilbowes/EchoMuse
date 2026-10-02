package speaker

import (
	"testing"

	"github.com/wilbowes/EchoMuse/internal/bindings/codec"
)

// The jack read-back rides the stats tick, so its whole job is to say what is
// TRUE — including "not measurable". Every case below is a way that could
// quietly become a wrong answer in a support bundle.

func TestJackReportReadsBackWhatTheCodecIsActuallyDoing(t *testing.T) {
	r := jackReport(1, true, map[string]string{
		ctlSpeakerAmp:      "Off",
		ctlHPDriverGain:    hpGainJack,
		codec.HPLDacSwitch: "1",
		codec.HPRDacSwitch: "1",
	}, 7)

	if r.Inserted == nil || !*r.Inserted {
		t.Errorf("h2w=1 is a plug in, got %v", r.Inserted)
	}
	if r.SpeakerAmp != "Off" {
		t.Errorf("want the amp as read back, got %q", r.SpeakerAmp)
	}
	if r.DriverGain == nil || *r.DriverGain != 11 {
		t.Errorf("want the jack gain as read back, got %v", r.DriverGain)
	}
	if r.DacL != "1" || r.DacR != "1" {
		t.Errorf("want both DAC routes as read back, got L=%q R=%q", r.DacL, r.DacR)
	}
	if r.DriftReapplies != 7 {
		t.Errorf("want the running correction count, got %d", r.DriftReapplies)
	}
}

// The whole point of the report. jack.Inserted() collapses "no detect switch"
// into false, so taking that answer directly would tell every board without
// one that nothing is plugged in.
func TestJackReportSaysUnpluggedOnlyWhenItCouldReadTheSwitch(t *testing.T) {
	r := jackReport(0, false, nil, 0)
	if r.Inserted != nil {
		t.Errorf("a board with no accdet must report absent, not %v", *r.Inserted)
	}
}

// 0 is the FLOOR of the jack gain's range and is exactly the fault this state
// was added to expose, so it must survive as 0 while an unreadable control
// stays absent. Flattening the two is what makes a bundle unreadable.
func TestJackReportDistinguishesZeroGainFromNoReading(t *testing.T) {
	floor := jackReport(1, true, map[string]string{ctlHPDriverGain: "0"}, 0)
	if floor.DriverGain == nil || *floor.DriverGain != 0 {
		t.Fatalf("a gain reading of 0 is a real reading, got %v", floor.DriverGain)
	}
	absent := jackReport(1, true, map[string]string{}, 0)
	if absent.DriverGain != nil {
		t.Errorf("an unreadable control must be absent, got %d", *absent.DriverGain)
	}
	if absent.SpeakerAmp != "" || absent.DacL != "" {
		t.Errorf("unreadable switches must be absent, got %q/%q", absent.SpeakerAmp, absent.DacL)
	}
}

// Same failure-to-look rule the drift path applies before it rewrites: a
// control this build cannot parse is absence, not a level.
func TestJackReportLeavesAnUnparseableGainAbsent(t *testing.T) {
	r := jackReport(1, true, map[string]string{ctlHPDriverGain: "Mute"}, 0)
	if r.DriverGain != nil {
		t.Errorf("an unparseable gain must not become a number, got %d", *r.DriverGain)
	}
}

// The reconcile may only ever rewrite the jack pair. A DAC route that has gone
// open is reported, not papered over by writing to it every 30s — that is a
// different change with audible consequences.
func TestJackReadBackCoversMoreThanTheReconcileDoes(t *testing.T) {
	if len(jackReadBack) <= len(jackControls) {
		t.Fatalf("the DAC routes are the point of the extra reads, got %v", jackReadBack)
	}
	for _, ctl := range jackReadBack {
		if ctl != codec.HPLDacSwitch && ctl != codec.HPRDacSwitch {
			continue
		}
		for _, w := range jackRouting(true) {
			if w.Ctl == ctl {
				t.Errorf("the reconcile must not write %s", ctl)
			}
		}
	}
}
