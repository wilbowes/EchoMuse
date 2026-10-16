package speaker

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bindings/mixer"
)

// Pins the DAC at the codec's 0dB, where stock leaves it — see the
// constant's doc comment. Above 127 this control adds digital gain to a
// chain whose limiter already puts peaks at full scale, which clips inside
// the DAC; a loudness difference belongs in the output chain instead.
func TestRadarDacUnityIsTheCodecsZeroDB(t *testing.T) {
	if radarDacUnity != "127" {
		t.Fatalf("radarDacUnity = %q, want \"127\"", radarDacUnity)
	}
}

func profileXML(values string) string {
	return `<mixercontrol><path name="ext_headphone_output" value="turnon"><kctl name="biquad coefficients" value="bad"/></path><path name="ext_speaker_output" value="turnon"><kctl name="biquad coefficients" value="` + values + `"/></path></mixercontrol>`
}
func TestRadarProfileSelectionAndValidation(t *testing.T) {
	good := strings.TrimSpace(strings.Repeat("123 ", 117))
	if p, e := radarSpeakerProfile([]byte(profileXML(good))); e != nil || len(p) != 117 || p[0] != "123" {
		t.Fatalf("profile=%v err=%v", p, e)
	}
	cases := map[string]string{
		"empty file":       "",
		"malformed XML":    "<broken",
		"short profile":    profileXML("1 2"),
		"silent profile":   profileXML(strings.Repeat("0 ", 117)),
		"out of range":     profileXML(strings.Repeat("256 ", 117)),
		"negative byte":    profileXML(strings.Repeat("-1 ", 117)),
		"headphones only":  strings.ReplaceAll(profileXML(good), "ext_speaker_output", "ext_headphone_output"),
		"wrong transition": strings.ReplaceAll(profileXML(good), "turnon", "turnoff"),
		"duplicate profile": strings.Replace(profileXML(good), "</mixercontrol>",
			`<path name="ext_speaker_output" value="turnon"><kctl name="biquad coefficients" value="1"/></path></mixercontrol>`, 1),
	}
	for name, data := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := radarSpeakerProfile([]byte(data)); err == nil {
				t.Fatal("accepted invalid profile")
			}
		})
	}
}

type radarMixer struct {
	writes []mixerWrite
	fail   string
}

func (m *radarMixer) Get(string) (string, error) { return "", nil }
func (m *radarMixer) Set(n string, v []string) error {
	m.writes = append(m.writes, mixerWrite{n, append([]string(nil), v...)})
	if n == m.fail {
		return errors.New("write failed")
	}
	return nil
}
func TestRadarPrepareFailsMuted(t *testing.T) {
	for _, failure := range []string{"missing", "invalid", "biquad coefficients", "Audio_I2S1_Setting"} {
		t.Run(failure, func(t *testing.T) {
			m := &radarMixer{fail: failure}
			mixer.Use(m)
			path := filepath.Join(t.TempDir(), "audio.xml")
			if failure != "missing" {
				body := profileXML(strings.Repeat("123 ", 117))
				if failure == "invalid" {
					body = "bad"
				}
				if err := os.WriteFile(path, []byte(body), 0600); err != nil {
					t.Fatal(err)
				}
			}
			if e := prepareRadarSpeaker(path); e == nil {
				t.Fatal("expected failure")
			}
			if m.writes[0].Ctl != radarMute || m.writes[0].Args[0] != "On" {
				t.Fatal("must mute first")
			}
			for _, w := range m.writes {
				if w.Ctl == radarMute && w.Args[0] == "Off" {
					t.Fatal("unmuted on error")
				}
			}
		})
	}
}
func TestRadarUnmuteSequenceAndStreamFailure(t *testing.T) {
	m := &radarMixer{}
	mixer.Use(m)
	var waits []time.Duration
	if e := unmuteRadarSpeaker(func(d time.Duration) error {
		waits = append(waits, d)
		if len(waits) == 1 && (len(m.writes) != 1 || m.writes[0].Ctl != mixer.SpeakerAmp) {
			t.Fatal("amp must precede settling")
		}
		if len(waits) == 2 && (m.writes[1].Ctl != mixer.PlaybackVolume || m.writes[1].Args[0] != "0" || m.writes[2].Ctl != radarMute || m.writes[2].Args[0] != "Off") {
			t.Fatal("release mute at gain zero")
		}
		return nil
	}); e != nil {
		t.Fatal(e)
	}
	if waits[0] != 3*time.Second || waits[1] != 4*time.Second {
		t.Fatal(waits)
	}
	last := m.writes[len(m.writes)-1]
	if last.Ctl != mixer.PlaybackVolume || last.Args[0] != radarDacUnity {
		t.Fatal(last)
	}
	for failWait := 1; failWait <= len(waits); failWait++ {
		m = &radarMixer{}
		mixer.Use(m)
		n := 0
		if e := unmuteRadarSpeaker(func(time.Duration) error {
			n++
			if n == failWait {
				return errors.New("PCM died")
			}
			return nil
		}); e == nil {
			t.Fatal("expected failure")
		}
		tail := m.writes[len(m.writes)-2:]
		if tail[0].Ctl != radarMute || tail[0].Args[0] != "On" || tail[1].Ctl != mixer.PlaybackVolume || tail[1].Args[0] != "0" {
			t.Fatal("failure did not remute", tail)
		}
	}
	for _, ctl := range []string{mixer.SpeakerAmp, radarMute, mixer.PlaybackVolume} {
		m = &radarMixer{fail: ctl}
		mixer.Use(m)
		if e := unmuteRadarSpeaker(func(time.Duration) error { return nil }); e == nil {
			t.Fatal("ignored failed mixer write")
		}
	}
}

// bothProfilesXML is a device file carrying both filter profiles, different
// from each other, so a test can tell which one was written.
func bothProfilesXML() (xml string, speaker, jack []string) {
	sp := strings.TrimSpace(strings.Repeat("123 ", 117))
	hp := strings.TrimSpace(strings.Repeat("7 ", 117))
	xml = `<mixercontrol>` +
		`<path name="ext_speaker_output" value="turnon"><kctl name="biquad coefficients" value="` + sp + `"/></path>` +
		`<path name="ext_headphone_output" value="turnon"><kctl name="biquad coefficients" value="` + hp + `"/></path>` +
		`<path name="ext_headphone_output" value="turnoff"><kctl name="Audio_DacMux_Setting" value="Off"/></path>` +
		`</mixercontrol>`
	return xml, strings.Fields(sp), strings.Fields(hp)
}

func TestRadarJackProfileReadsTheHeadphonePath(t *testing.T) {
	data, sp, hp := bothProfilesXML()
	got, err := radarJackProfile([]byte(data))
	if err != nil || strings.Join(got, " ") != strings.Join(hp, " ") {
		t.Fatalf("jack profile=%v err=%v", got, err)
	}
	if got, _ := radarSpeakerProfile([]byte(data)); strings.Join(got, " ") != strings.Join(sp, " ") {
		t.Fatalf("speaker profile picked up the jack path: %v", got)
	}
	// profileXML's headphone path is invalid ("bad"): refused, not guessed.
	if _, err := radarJackProfile([]byte(profileXML(strings.Repeat("1 ", 117)))); err == nil {
		t.Fatal("accepted an invalid jack profile")
	}
}

func indexOf(ws []mixerWrite, ctl string) int {
	for i, w := range ws {
		if w.Ctl == ctl {
			return i
		}
	}
	return -1
}

// The HAL's order and the HAL's values: amp off first and the jack filter on
// insert; the speaker filter back and the amp on LAST on removal, so the
// speaker never plays through the jack filter.
func TestRadarJackRoutingSwapsTheFilterInTheHALsOrder(t *testing.T) {
	_, sp, hp := bothProfilesXML()

	in := radarJackRouting(true, sp, hp)
	if in[0].Ctl != ctlSpeakerAmp || in[0].Args[0] != "Off" {
		t.Errorf("insert must switch the amp off first, got %+v", in[0])
	}
	if i := indexOf(in, ctlBiquad); i < 0 || strings.Join(in[i].Args, " ") != strings.Join(hp, " ") {
		t.Errorf("insert must load the jack filter, got %+v", in)
	}
	if i := indexOf(in, ctlIgnoreRampUp); i < 0 || in[i].Args[0] != "On" {
		t.Errorf("insert must set Ignore Ramp Up On, got %+v", in)
	}

	out := radarJackRouting(false, sp, hp)
	last := out[len(out)-1]
	if last.Ctl != ctlSpeakerAmp || last.Args[0] != "On" {
		t.Errorf("removal must switch the amp on last, got %+v", last)
	}
	if i := indexOf(out, ctlBiquad); i < 0 || strings.Join(out[i].Args, " ") != strings.Join(sp, " ") {
		t.Errorf("removal must restore the speaker filter, got %+v", out)
	}
	if i := indexOf(out, ctlIgnoreRampUp); i < 0 || out[i].Args[0] != "Off" {
		t.Errorf("removal must set Ignore Ramp Up Off, got %+v", out)
	}
}

// The controls the reconcile loop checks (jackRoutingDrift, which works from
// jackRouting) must get the same values on Radar, or it would rewrite them
// every 30s against the routing it just applied.
func TestRadarJackRoutingAgreesWithTheSharedControls(t *testing.T) {
	_, sp, hp := bothProfilesXML()
	for _, inserted := range []bool{true, false} {
		radar := radarJackRouting(inserted, sp, hp)
		for _, w := range jackRouting(inserted) {
			i := indexOf(radar, w.Ctl)
			if i < 0 || strings.Join(radar[i].Args, " ") != strings.Join(w.Args, " ") {
				t.Errorf("inserted=%v: %s is %+v on Radar, %v in jackRouting", inserted, w.Ctl, radar, w.Args)
			}
		}
	}
}

// No jack profile: exactly the previous behaviour, never half of the new one.
func TestRadarJackRoutingWithoutProfilesIsTheOldRouting(t *testing.T) {
	_, sp, hp := bothProfilesXML()
	for _, inserted := range []bool{true, false} {
		for _, c := range [][2][]string{{nil, hp}, {sp, nil}, {nil, nil}} {
			got := radarJackRouting(inserted, c[0], c[1])
			want := jackRouting(inserted)
			if len(got) != len(want) || indexOf(got, ctlBiquad) >= 0 {
				t.Errorf("inserted=%v: got %+v, want %+v", inserted, got, want)
			}
		}
	}
}

func TestLoadRadarJackProfilesNeedsBoth(t *testing.T) {
	dir := t.TempDir()
	both, sp, hp := bothProfilesXML()
	write := func(name, data string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(data), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}
	s, j := loadRadarJackProfiles(write("both.xml", both))
	if strings.Join(s, " ") != strings.Join(sp, " ") || strings.Join(j, " ") != strings.Join(hp, " ") {
		t.Fatalf("speaker=%v jack=%v", s, j)
	}
	noJack := strings.ReplaceAll(both, `name="ext_headphone_output" value="turnon"`, `name="ext_headphone_output" value="turnoff"`)
	if s, j := loadRadarJackProfiles(write("nojack.xml", noJack)); s != nil || j != nil {
		t.Errorf("a file without a jack profile must give neither, got %v %v", s, j)
	}
	if s, j := loadRadarJackProfiles(filepath.Join(dir, "missing.xml")); s != nil || j != nil {
		t.Errorf("a missing file must give neither, got %v %v", s, j)
	}
}
