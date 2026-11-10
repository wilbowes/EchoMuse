package speaker

import (
	"errors"
	"strings"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// halXML is shaped like an audio HAL's audio_device.xml, with made-up values:
// stock's own never enter the repository.
func halXML(speaker, jack string) string {
	return `<?xml version="1.0" encoding="UTF-8"?>
<mixercontrol>
    <kctl name="HP Driver Gain Volume" value="0" />
    <path name="ext_speaker_output" value="turnon">
        <kctl name="Audio_DacMux_Setting" value="Off" />
` + speaker + `
        <kctl name="Ext_Speaker_Amp_Switch" value="On" />
    </path>
    <path name="ext_speaker_output" value="turnoff">
        <kctl name="filter" value="9 9 9 9" />
    </path>
    <path name="ext_headphone_output" value="turnon">
` + jack + `
    </path>
</mixercontrol>`
}

func filterKctl(v string) string { return `<kctl name="filter" value="` + v + `" />` }

var testSpec = board.Speaker{
	AmpMute: "Amp Mute",
	Filter: &board.CodecFilter{
		Control: "filter", Bytes: 4, File: "/hal.xml",
		Speaker: "ext_speaker_output", Jack: "ext_headphone_output",
	},
	Setup: []board.Ctl{{Name: "DRC", Values: []string{"Disabled"}}},
	Jack:  biscuitJack,
}

func reader(content string) func(string) ([]byte, error) {
	return func(p string) ([]byte, error) {
		if p != "/hal.xml" {
			return nil, errors.New("no such file")
		}
		return []byte(content), nil
	}
}

func TestOutputReadsEachFilterFromItsOwnTurnon(t *testing.T) {
	o, err := loadOutput(testSpec, reader(halXML(filterKctl("1 2 3 4"), filterKctl("5 6 7 255"))))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(o.speakerFilter, " ") != "1 2 3 4" || strings.Join(o.jackFilter, " ") != "5 6 7 255" {
		t.Fatalf("speaker %v jack %v", o.speakerFilter, o.jackFilter)
	}
	s := o.setup()
	if len(s) != 2 || s[0].Ctl != "filter" || s[1].Ctl != "DRC" {
		t.Errorf("setup %+v: the filter, then the board's controls", s)
	}
}

// Every way the file can be wrong leaves the speaker without a filter and
// says why. All zeros is the codec's reset state, which is silence.
func TestOutputRefusesABadSpeakerFilter(t *testing.T) {
	for name, kctl := range map[string]string{
		"missing":   ``,
		"duplicate": filterKctl("1 2 3 4") + filterKctl("1 2 3 4"),
		"short":     filterKctl("1 2 3"),
		"long":      filterKctl("1 2 3 4 5"),
		"range":     filterKctl("1 2 3 256"),
		"not a num": filterKctl("1 2 3 x"),
		"all zero":  filterKctl("0 0 0 0"),
	} {
		o, err := loadOutput(testSpec, reader(halXML(kctl, filterKctl("5 6 7 8"))))
		if err == nil || o.speakerFilter != nil {
			t.Errorf("%s: accepted (%v)", name, o.speakerFilter)
		}
		if o.spec.AmpMute == "" {
			t.Errorf("%s: lost the rest of the board's spec", name)
		}
	}
	if _, err := loadOutput(testSpec, func(string) ([]byte, error) { return nil, errors.New("gone") }); err == nil {
		t.Error("unreadable file accepted")
	}
	if _, err := loadOutput(testSpec, reader("<mixercontrol><path")); err == nil {
		t.Error("malformed XML accepted")
	}
}

// A jack path that cannot be read keeps the speaker's filter on the jack, as
// before there was a swap, and reports it.
func TestOutputKeepsTheSpeakerFilterWithoutAJackOne(t *testing.T) {
	o, err := loadOutput(testSpec, reader(halXML(filterKctl("1 2 3 4"), "")))
	if err == nil {
		t.Error("a missing jack filter must be reported")
	}
	if strings.Join(o.jackFilter, " ") != "1 2 3 4" {
		t.Errorf("jack filter %v, want the speaker's", o.jackFilter)
	}
}

// The filter is swapped while the amp is off: after it goes off on insert,
// before it comes on at removal.
func TestJackSwapsTheFilterWhileTheAmpIsOff(t *testing.T) {
	o, err := loadOutput(testSpec, reader(halXML(filterKctl("1 2 3 4"), filterKctl("5 6 7 8"))))
	if err != nil {
		t.Fatal(err)
	}
	pos := func(ws []mixerWrite, ctl string) int {
		for i, w := range ws {
			if w.Ctl == ctl {
				return i
			}
		}
		return -1
	}
	in, out := o.jack(true), o.jack(false)
	if f, a := pos(in, "filter"), pos(in, amp); f < 0 || a < 0 || f < a || in[f].Args[0] != "5" {
		t.Errorf("insert %+v", in)
	}
	if f, a := pos(out, "filter"), pos(out, amp); f < 0 || a < 0 || f > a || out[f].Args[0] != "1" {
		t.Errorf("removal %+v", out)
	}
}

// A board without a filter or a mute plays its jack table exactly.
func TestOutputWithoutAFilterIsTheBoardsTable(t *testing.T) {
	o, err := loadOutput(board.Speaker{Jack: biscuitJack}, nil)
	if err != nil || len(o.setup()) != 0 {
		t.Fatalf("setup %+v, %v", o.setup(), err)
	}
	for _, in := range []bool{true, false} {
		got, want := o.jack(in), jackRouting(biscuitJack, in)
		if len(got) != len(want) {
			t.Errorf("inserted=%v: %+v, want %+v", in, got, want)
		}
	}
	if o.releaseMute() != nil {
		t.Error("no mute control must be a no-op")
	}
}

func TestGuardBandFollowsTheBoard(t *testing.T) {
	if guardBand(board.BassBand{}) != outchain.DefaultGuardBand {
		t.Error("a board stating no band must keep biscuit's")
	}
	for _, b := range board.Known {
		want := b.Hardware.Speaker.BassGuard
		if g := guardBand(want); g.CrossoverHz != want.CrossoverHz || g.ThresholdDb != want.ThresholdDb {
			t.Errorf("%s: %+v, want %+v", b.ID, g, want)
		}
	}
	if guardBand(board.Biscuit.Hardware.Speaker.BassGuard) != outchain.DefaultGuardBand {
		t.Error("biscuit's band must be the default the controller's em_mbc also runs")
	}
}

// Each board mutes and unmutes its own volume control at its own values, and
// a board with no amp switch has nothing written for it: the Dot 3's TAS2770
// is silenced by its volume, and biscuit's 127 there would be -64dB.
func TestOutputUsesTheBoardsVolumeAndAmp(t *testing.T) {
	for _, c := range []struct {
		b           *board.Board
		unity, amps string
	}{
		{board.Biscuit, "127", "Ext_Speaker_Amp_Switch"},
		{board.Radar, "127", "Ext_Speaker_Amp_Switch"},
		{board.Donut, "255", ""},
	} {
		o := output{spec: c.b.Hardware.Speaker}
		if m, u := o.mute(), o.unity(); m.Ctl != "PCM Playback Volume" || m.Args[0] != "0" || u.Args[0] != c.unity {
			t.Errorf("%s: mute %+v unity %+v", c.b.ID, m, u)
		}
		on, off := o.amp(true), o.amp(false)
		if c.amps == "" {
			if on != nil || off != nil {
				t.Errorf("%s: amp writes %+v %+v on a board with no switch", c.b.ID, on, off)
			}
			continue
		}
		if len(on) != 1 || on[0].Ctl != c.amps || on[0].Args[0] != "On" || off[0].Args[0] != "Off" {
			t.Errorf("%s: amp %+v %+v", c.b.ID, on, off)
		}
	}
}
