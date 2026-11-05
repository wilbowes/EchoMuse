package speaker

import (
	"strings"
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/board"
)

// The jack read-back rides the stats tick, so its whole job is to say what is
// TRUE — including "not measurable". Every case below is a way that could
// quietly become a wrong answer in a support bundle.

func TestJackReportReadsBackWhatTheCodecIsActuallyDoing(t *testing.T) {
	r := jackReport(1, true, map[string]string{
		"Ext_Speaker_Amp_Switch":        "Off",
		"HP Driver Gain Volume":         "11",
		"HPL Output Mixer L_DAC Switch": "1",
		"HPR Output Mixer R_DAC Switch": "1",
	}, 7)

	if r.Inserted == nil || !*r.Inserted {
		t.Errorf("h2w=1 is a plug in, got %v", r.Inserted)
	}
	if got := r.Controls["Ext_Speaker_Amp_Switch"]; got != "Off" {
		t.Errorf("want the amp as read back, got %q", got)
	}
	if got := r.Controls["HP Driver Gain Volume"]; got != "11" {
		t.Errorf("want the gain as read back, got %q", got)
	}
	if r.Controls["HPL Output Mixer L_DAC Switch"] != "1" || r.Controls["HPR Output Mixer R_DAC Switch"] != "1" {
		t.Errorf("want both DAC routes as read back, got %v", r.Controls)
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
	if r.Controls != nil {
		t.Errorf("nothing was read, so no control may be reported, got %v", r.Controls)
	}
}

// 0 is the floor of the jack gain's range and is exactly the fault this state
// was added to expose, so it must survive as 0 while an unreadable control
// stays absent. Flattening the two is what makes a bundle unreadable.
func TestJackReportDistinguishesZeroGainFromNoReading(t *testing.T) {
	floor := jackReport(1, true, map[string]string{"HP Driver Gain Volume": "0"}, 0)
	if got, ok := floor.Controls["HP Driver Gain Volume"]; !ok || got != "0" {
		t.Fatalf("a gain reading of 0 is a real reading, got %q (present %v)", got, ok)
	}
	absent := jackReport(1, true, map[string]string{}, 0)
	if _, ok := absent.Controls["HP Driver Gain Volume"]; ok {
		t.Errorf("an unreadable control must be absent")
	}
}

// The reconcile may only ever rewrite the board's jack pair. A DAC route that
// has gone open is reported, not papered over by writing to it every 30s.
func TestJackReadBackCoversMoreThanTheReconcileDoes(t *testing.T) {
	names := jackReadBack(
		[]string{"Ext_Speaker_Amp_Switch"}, []string{"Ext_Speaker_Amp_Switch"},
		[]string{"HPR Output Mixer R_DAC Switch", "HPL Output Mixer L_DAC Switch", "ADC_A DIF1_L Input Gain"})
	if len(names) != 3 {
		t.Fatalf("want the one board control plus both DAC routes, got %v", names)
	}
	j := board.Jack{Insert: []board.Ctl{{Name: "Ext_Speaker_Amp_Switch", Values: []string{"0"}}}}
	for _, w := range jackRouting(j, true) {
		if strings.Contains(w.Ctl, "DAC Switch") {
			t.Errorf("the reconcile must not write %s", w.Ctl)
		}
	}
}
