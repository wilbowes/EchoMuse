package speaker

import (
	"encoding/xml"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bindings/mixer"
)

const radarMute = "MFP Gpio Mute"

// radarDeviceXML is the device's own audio configuration on its stock system
// partition. Both codec filter profiles are read from it at runtime, so no
// stock values are compiled into or distributed with a build.
const radarDeviceXML = "/system/etc/audio_device.xml"

// Read the device's own speaker calibration, not the headphone profile, and
// never redistribute stock files in a build. FireOS's HAL normally loads this;
// emOS has no HAL. An all-zero profile leaves Radar silent (#535).
func radarSpeakerProfile(data []byte) ([]string, error) {
	return radarProfile(data, "ext_speaker_output", "speaker")
}

// radarJackProfile is the codec filter Amazon's HAL loads when a plug goes
// into the jack (ext_headphone_output/turnon). The speaker profile is the
// Radar driver's correction; left in place it shapes the line out too.
func radarJackProfile(data []byte) ([]string, error) {
	return radarProfile(data, "ext_headphone_output", "jack")
}

func radarProfile(data []byte, pathName, label string) ([]string, error) {
	var doc struct {
		XMLName xml.Name `xml:"mixercontrol"`
		Paths   []struct {
			Name     string `xml:"name,attr"`
			Value    string `xml:"value,attr"`
			Controls []struct {
				Name  string `xml:"name,attr"`
				Value string `xml:"value,attr"`
			} `xml:"kctl"`
		} `xml:"path"`
	}
	if err := xml.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	var profile []string
	for _, path := range doc.Paths {
		if path.Name != pathName || path.Value != "turnon" {
			continue
		}
		for _, ctl := range path.Controls {
			if ctl.Name != "biquad coefficients" {
				continue
			}
			if profile != nil {
				return nil, fmt.Errorf("duplicate Radar %s profile", label)
			}
			profile = strings.Fields(ctl.Value)
		}
	}
	if len(profile) != 117 {
		return nil, fmt.Errorf("Radar %s profile: expected 117 bytes, got %d", label, len(profile))
	}
	nonzero := false
	for _, s := range profile {
		v, err := strconv.ParseUint(s, 10, 8)
		if err != nil {
			return nil, fmt.Errorf("Radar %s profile: invalid byte %q", label, s)
		}
		nonzero = nonzero || v != 0
	}
	if !nonzero {
		return nil, fmt.Errorf("Radar %s profile is all zero", label)
	}
	return profile, nil
}

// Keep physical mute asserted before any codec/clock changes. Failure must
// leave the speaker muted, never release an unconfigured DAC at full gain.
func prepareRadarSpeaker(path string) error {
	if err := mixer.Set(radarMute, "On"); err != nil {
		return err
	}
	if err := mixer.Set(mixer.PlaybackVolume, "0"); err != nil {
		return err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	profile, err := radarSpeakerProfile(data)
	if err != nil {
		return err
	}
	for _, w := range []mixerWrite{
		{mixer.SpeakerAmp, []string{"Off"}},
		{"biquad coefficients", profile},
		{"Audio_I2S1_Setting", []string{"On"}},
		{"HP DAC Playback Switch", []string{"1", "1"}},
		{"Audio_DacMux_Setting", []string{"Off"}},
		{"DRC Control", []string{"Disabled"}},
		{mixer.HPDriverGain, []string{"6", "6"}},
	} {
		if err := mixer.Set(w.Ctl, w.Args...); err != nil {
			return err
		}
	}
	return nil
}

// radarDacUnity is Radar's DAC digital-volume baseline: 127, the
// tlv320aic32x4's 0dB point, which is what stock runs it at. Kept separate
// from dacUnity (pcm_speaker.go) because Init branches to startRadarOutput
// before that is read, not because the value differs.
//
// It was 150, then 140, then 145 (38a874d, 8d33d2b, 292b168), and the
// reasoning behind that was wrong in two places, both checkable in
// device/tools/radar_dump:
//
//   - Radar's playback DAC is NOT a different chip. It is the same
//     tlv320aic32x4 as biscuit (2-0018), whose "PCM Playback Volume" runs
//     0..175 in 0.5dB steps with 0dB at 127 — 175 being the control's
//     maximum is also why that was where the bench "browned out".
//   - Stock does NOT run it at 255. 255 appears only in mixer_paths.xml,
//     a generic MediaTek file whose other controls (Audio Amp Playback
//     Volume, LINEOUT Mux, HPOUT Mux, I2S O03_O04 Switch, AIF TX Mux) do
//     not exist on this codec at all, and 255 is outside the control's
//     range. The HAL's real file, audio_device.xml, never writes it, and
//     the stock tinymix dump reads 127 127. Stock attenuates in
//     AudioFlinger and leaves the DAC at its reset 0dB, exactly as on
//     biscuit (device/CLAUDE.md, volume section).
//
// 145 still sounded right by ear because it was making up for gain the
// output chain did not have yet: MBCL's +4dB system gain and band 3/4's
// +3dB trims (8239c66), OutputTrim's +3dB and ParametricEQ (f21b20f), and
// the volume-matched EQ file (EQ_100 is 2dB hotter at 1kHz than EQ_50).
// With those ported, the chain's output measured 8-13dB hotter in RMS than
// the chain 145 was tuned against, peaks reach 0dBFS at full volume, and
// 145 would add +9dB on top INSIDE the DAC — where biscuit measured THD of
// 65% at 153 on near-full-scale input. Any level difference against a
// stock Echo from here belongs in the chain, which is in software and
// measurable, never in DAC gain above 0dB.
const radarDacUnity = "127"

// radarDacUnityLevel is radarDacUnity as an int, for the ramp loop below —
// ONE source for both, deliberately: the loop bound used to be a second,
// separately-maintained literal ("150"), which is exactly how this value
// drifted out of sync with itself the first time this was tuned.
var radarDacUnityLevel = func() int {
	n, err := strconv.Atoi(radarDacUnity)
	if err != nil {
		panic("radarDacUnity must parse as an int: " + err.Error())
	}
	return n
}()

// The measured quiet-start sequence from the Radar bench: clock silence for
// three seconds at gain zero, release physical mute, wait four seconds, then
// ramp the DAC. User volume remains in software; Init has not returned yet,
// so no caller can enqueue audio or cues during this sequence.
func unmuteRadarSpeaker(wait func(time.Duration) error) (err error) {
	defer func() {
		if err != nil {
			mixer.Set(radarMute, "On")
			mixer.Set(mixer.PlaybackVolume, "0")
		}
	}()
	if err = mixer.Set(mixer.SpeakerAmp, "On"); err != nil {
		return err
	}
	if err = wait(3 * time.Second); err != nil {
		return err
	}
	if err = mixer.Set(mixer.PlaybackVolume, "0"); err != nil {
		return err
	}
	if err = mixer.Set(radarMute, "Off"); err != nil {
		return err
	}
	if err = wait(4 * time.Second); err != nil {
		return err
	}
	for v := 10; v < radarDacUnityLevel; v += 10 {
		if err = mixer.Set(mixer.PlaybackVolume, strconv.Itoa(v)); err != nil {
			return err
		}
		if err = wait(50 * time.Millisecond); err != nil {
			return err
		}
	}
	return mixer.Set(mixer.PlaybackVolume, radarDacUnity)
}

// Radar's jack, from the device's own audio_device.xml and the HAL behind it
// (audio.primary.mt8163_headless.so), read on Radar1, 2026-10-10.
const (
	ctlBiquad       = "biquad coefficients"
	ctlIgnoreRampUp = "Ignore Ramp Up"
)

// radarJackRouting is jackRouting for Radar: the same three controls with the
// same values, plus the two the HAL's paths add.
//
//   - The codec filter is swapped. ext_headphone_output/turnon loads its own
//     profile; the speaker one is the driver correction for Radar's woofer and
//     tweeter, and left in place it shapes the line out ("sounds odd").
//     Removal writes the speaker profile back, which Init otherwise only does
//     once at boot.
//   - Ignore Ramp Up is On with a plug in and Off without, as the two paths
//     set it.
//
// The order is the HAL's: inserting runs ext_speaker_output/turnoff, then
// ext_headphone_output/turnon; removing runs ext_headphone_output/turnoff,
// then ext_speaker_output/turnon. So the amp goes off first on insert and on
// last on removal, and the speaker never plays through the jack filter.
//
// DacMux is On with a plug in, as on the Dot (#566): with the fork's Off the
// jack was silent on Radar1 (2026-10-10), and the HAL writes On as well.
//
// Without both profiles this is jackRouting unchanged: a missing jack path
// keeps the previous behaviour rather than writing half a configuration.
func radarJackRouting(inserted bool, speaker, jack []string) []mixerWrite {
	if speaker == nil || jack == nil {
		return jackRouting(inserted)
	}
	if inserted {
		return []mixerWrite{
			{Ctl: ctlSpeakerAmp, Args: []string{"Off"}},
			{Ctl: ctlBiquad, Args: jack},
			{Ctl: ctlIgnoreRampUp, Args: []string{"On"}},
			{Ctl: ctlHPDriverGain, Args: []string{hpGainJack, hpGainJack}},
			{Ctl: ctlDacMux, Args: []string{dacMuxJack}},
		}
	}
	return []mixerWrite{
		{Ctl: ctlIgnoreRampUp, Args: []string{"Off"}},
		{Ctl: ctlDacMux, Args: []string{dacMuxInternal}},
		{Ctl: ctlBiquad, Args: speaker},
		{Ctl: ctlHPDriverGain, Args: []string{hpGainInternal, hpGainInternal}},
		{Ctl: ctlSpeakerAmp, Args: []string{"On"}},
	}
}

// loadRadarJackProfiles reads both filter profiles for the jack routing. The
// speaker profile has already been validated by prepareRadarSpeaker; a jack
// profile that is missing or invalid is logged and the jack keeps the speaker
// filter, which is how every build before this one behaved.
func loadRadarJackProfiles(path string) (speaker, jack []string) {
	data, err := os.ReadFile(path)
	if err != nil {
		log.Printf("[speaker] Radar jack profile unavailable (%v) — the jack keeps the speaker filter", err)
		return nil, nil
	}
	if speaker, err = radarSpeakerProfile(data); err != nil {
		log.Printf("[speaker] Radar jack routing: %v — the jack keeps the speaker filter", err)
		return nil, nil
	}
	if jack, err = radarJackProfile(data); err != nil {
		log.Printf("[speaker] Radar jack profile unavailable (%v) — the jack keeps the speaker filter", err)
		return nil, nil
	}
	return speaker, jack
}
