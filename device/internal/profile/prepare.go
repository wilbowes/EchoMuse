package profile

import (
	"fmt"
	"log"
	"os/exec"
	"strconv"

	"github.com/wilbowes/EchoMuse/internal/alsa"
)

// Prepare takes ownership of the audio hardware: it stops the init services
// that would otherwise hold the PCMs, then applies the mixer init sequence.
//
// Call once at startup, before any PCM is opened. Stopping the services is
// best-effort, since a missing service is not an error and the set differs
// between Fire OS and LineageOS.
func (p *Profile) Prepare() error {
	for _, svc := range p.StopServices {
		if err := exec.Command("stop", svc).Run(); err != nil {
			log.Printf("profile: stop %s: %v (continuing)", svc, err)
		}
	}
	// Applied best-effort. Board revisions differ in which controls exist, and
	// on Fire OS the boot script has already written the same values and
	// ignored any errors.
	init := make([]alsa.Control, 0, len(p.MixerInit))
	for _, c := range p.MixerInit {
		c.Optional = true
		init = append(init, c)
	}
	if err := alsa.Apply(p.Mic.Card, init); err != nil {
		return fmt.Errorf("mixer init for %s: %w", p.Name, err)
	}
	log.Printf("profile: %s prepared (%d mixer controls, AEC reference: %v)",
		p.Name, len(p.MixerInit), p.HasAECReference())
	return nil
}

// ReleaseServices restarts the init services Prepare stopped, handing the
// audio hardware back to Android. The daemon does not need this: it owns the
// device for its lifetime: but bring-up tooling must leave a usable system
// rather than a silent one.
func (p *Profile) ReleaseServices() {
	for i := len(p.StopServices) - 1; i >= 0; i-- {
		if err := exec.Command("start", p.StopServices[i]).Run(); err != nil {
			log.Printf("profile: start %s: %v", p.StopServices[i], err)
		}
	}
}

// EnableAmp puts the output stage into its audible state. Cheap enough to
// call at the start of every stream, which is necessary where another audio
// stack shares the same mixer and resets the control behind us.
func (p *Profile) EnableAmp() {
	if len(p.AmpOn) == 0 {
		return
	}
	if err := alsa.Apply(p.Mic.Card, p.AmpOn); err != nil {
		log.Printf("profile: amp on: %v", err)
	}
}

// SilenceAmp applies the amp-off sequence. The server does this on shutdown;
// without it an enabled amp on an idle DAC hisses audibly for as long as the
// daemon is down.
func (p *Profile) SilenceAmp() {
	if len(p.AmpOff) == 0 {
		return
	}
	if err := alsa.Apply(p.Mic.Card, p.AmpOff); err != nil {
		log.Printf("profile: amp off: %v", err)
	}
}

// SetMicGain retunes the capture gain from a config push. Values of 0 leave
// that control alone, matching the previous "only apply what was sent"
// behaviour. Both controls are stereo, so each value is written twice.
//
// Which controls these are is per-device: biscuit has four ADCs that must move
// together, checkers has one.
func (p *Profile) SetMicGain(digital, micpga int) {
	apply := func(ctls []alsa.Control, v int) {
		if v <= 0 || len(ctls) == 0 {
			return
		}
		val := strconv.Itoa(v)
		out := make([]alsa.Control, 0, len(ctls))
		for _, c := range ctls {
			c.Values = []string{val, val}
			c.Optional = true // a missing control must not kill a config push
			out = append(out, c)
		}
		if err := alsa.Apply(p.Mic.Card, out); err != nil {
			log.Printf("profile: mic gain: %v", err)
		}
	}
	apply(p.AdcDigitalGainCtls, digital)
	apply(p.AdcMicpgaCtls, micpga)
}

// Verify checks the profile against what the driver reports, so a
// wrong constant fails loudly at startup instead of producing silence or
// garbled audio. Returns a descriptive error listing every mismatch.
func (p *Profile) Verify() error {
	var problems []string

	if caps, err := alsa.Capabilities(p.Mic.Card, p.Mic.Device, false); err != nil {
		problems = append(problems, fmt.Sprintf("mic capabilities: %v", err))
	} else {
		if uint32(p.Mic.Channels) < caps.ChannelsMin || uint32(p.Mic.Channels) > caps.ChannelsMax {
			problems = append(problems, fmt.Sprintf(
				"mic channels %d outside driver range %d..%d",
				p.Mic.Channels, caps.ChannelsMin, caps.ChannelsMax))
		}
		if uint32(p.Mic.SampleRate) < caps.RateMin || uint32(p.Mic.SampleRate) > caps.RateMax {
			problems = append(problems, fmt.Sprintf(
				"mic rate %d outside driver range %d..%d",
				p.Mic.SampleRate, caps.RateMin, caps.RateMax))
		}
		if !hasFormat(caps.Formats, p.Mic.Format) {
			problems = append(problems, fmt.Sprintf(
				"mic format %v not supported (driver offers %v)", p.Mic.Format, caps.Formats))
		}
	}

	for _, ch := range append(append([]int{}, p.Mic.MicChannels...), p.Mic.RefChannels...) {
		if ch < 0 || ch >= p.Mic.Channels {
			problems = append(problems, fmt.Sprintf(
				"channel index %d outside 0..%d", ch, p.Mic.Channels-1))
		}
	}

	if len(problems) > 0 {
		return fmt.Errorf("profile %s: %v", p.Name, problems)
	}
	return nil
}

func hasFormat(list []alsa.Format, f alsa.Format) bool {
	for _, v := range list {
		if v == f {
			return true
		}
	}
	return false
}
