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
	if last.Ctl != mixer.PlaybackVolume || last.Args[0] != "127" {
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
