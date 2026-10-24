package speaker

import (
	"encoding/xml"
	"fmt"
	"strconv"
	"strings"

	"github.com/wilbowes/EchoMuse/internal/bindings/mixer"
	"github.com/wilbowes/EchoMuse/internal/outchain"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// output is a board's own playback needs (board.Speaker) as mixer writes,
// with its filter values read off the device. Host-testable, since
// pcm_speaker.go is ARM-only.
type output struct {
	spec board.Speaker
	// speakerFilter and jackFilter are the filter's values for each output,
	// nil when the board has no filter. jackFilter falls back to the
	// speaker's when the config has none for the jack.
	speakerFilter, jackFilter []string
}

// loadOutput reads spec's filter through read. On error the output still
// carries the rest of spec: a board whose filter cannot be read plays
// unfiltered, or silent, and says so, rather than taking the whole device
// down with its speaker.
func loadOutput(spec board.Speaker, read func(string) ([]byte, error)) (output, error) {
	o := output{spec: spec}
	f := spec.Filter
	if f == nil {
		return o, nil
	}
	data, err := read(f.File)
	if err != nil {
		return o, fmt.Errorf("speaker filter: %w", err)
	}
	if o.speakerFilter, err = halFilter(data, f, f.Speaker); err != nil {
		return o, fmt.Errorf("speaker filter: %w", err)
	}
	o.jackFilter = o.speakerFilter
	if f.Jack != "" {
		if j, err := halFilter(data, f, f.Jack); err == nil {
			o.jackFilter = j
		} else {
			return o, fmt.Errorf("jack filter (keeping the speaker's): %w", err)
		}
	}
	return o, nil
}

// halFilter returns f's values from the "turnon" of path in an audio HAL
// mixer config, which is a list of <path name value> of <kctl name value>.
func halFilter(data []byte, f *board.CodecFilter, path string) ([]string, error) {
	var doc struct {
		Paths []struct {
			Name  string `xml:"name,attr"`
			Value string `xml:"value,attr"`
			Ctls  []struct {
				Name  string `xml:"name,attr"`
				Value string `xml:"value,attr"`
			} `xml:"kctl"`
		} `xml:"path"`
	}
	if err := xml.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("%s: %w", f.File, err)
	}
	var vals []string
	found := 0
	for _, p := range doc.Paths {
		if p.Name != path || p.Value != "turnon" {
			continue
		}
		for _, c := range p.Ctls {
			if c.Name == f.Control {
				vals = strings.Fields(c.Value)
				found++
			}
		}
	}
	if found != 1 {
		return nil, fmt.Errorf("%s: %d %q in %s/turnon, want 1", f.File, found, f.Control, path)
	}
	if len(vals) != f.Bytes {
		return nil, fmt.Errorf("%s: %s %q has %d bytes, want %d", f.File, path, f.Control, len(vals), f.Bytes)
	}
	zero := true
	for _, v := range vals {
		n, err := strconv.ParseUint(v, 10, 8)
		if err != nil {
			return nil, fmt.Errorf("%s: %s %q: bad byte %q", f.File, path, f.Control, v)
		}
		zero = zero && n == 0
	}
	// The codec's reset state, and silence.
	if zero {
		return nil, fmt.Errorf("%s: %s %q is all zeros", f.File, path, f.Control)
	}
	return vals, nil
}

// setup is written once before the stream opens: the speaker's filter, then
// the board's own controls.
func (o output) setup() []mixerWrite {
	var ws []mixerWrite
	if o.speakerFilter != nil {
		ws = append(ws, mixerWrite{Ctl: o.spec.Filter.Control, Args: o.speakerFilter})
	}
	for _, c := range o.spec.Setup {
		ws = append(ws, mixerWrite{Ctl: c.Name, Args: c.Values})
	}
	return ws
}

// jack is jackRouting with the filter swapped for the output in use. The
// filter goes in after the amp is off and before it comes back on, so the
// speaker never plays through the jack's filter, nor the jack through the
// speaker's correction.
func (o output) jack(inserted bool) []mixerWrite {
	ws := jackRouting(o.spec.Jack, inserted)
	if o.speakerFilter == nil {
		return ws
	}
	if inserted {
		return append(ws, mixerWrite{Ctl: o.spec.Filter.Control, Args: o.jackFilter})
	}
	return append([]mixerWrite{{Ctl: o.spec.Filter.Control, Args: o.speakerFilter}}, ws...)
}

// releaseMute lets the amplifier play. The codec driver mutes it at every
// stream open and close (measured on radar, 2026-10-23), so this follows the
// stream starting; an underrun's recovery leaves it as it was.
func (o output) releaseMute() error {
	if o.spec.AmpMute == "" {
		return nil
	}
	return mixer.Set(o.spec.AmpMute, "Off")
}

// holdMute silences the amplifier in hardware ahead of anything that could
// click.
func (o output) holdMute() {
	if o.spec.AmpMute != "" {
		mixer.Set(o.spec.AmpMute, "On")
	}
}

// mute and unity are the hardware volume's silent and 0dB settings.
func (o output) mute() mixerWrite {
	return mixerWrite{Ctl: o.spec.Volume.Control, Args: []string{o.spec.Volume.Mute}}
}

func (o output) unity() mixerWrite {
	return mixerWrite{Ctl: o.spec.Volume.Control, Args: []string{o.spec.Volume.Unity}}
}

// amp switches the amplifier, nothing on a board that has no switch.
func (o output) amp(on bool) []mixerWrite {
	if o.spec.AmpSwitch == "" {
		return nil
	}
	v := "Off"
	if on {
		v = "On"
	}
	return []mixerWrite{{Ctl: o.spec.AmpSwitch, Args: []string{v}}}
}

// guardBand is a board's bass band as the output chain takes it; a board
// that states none gets biscuit's, as every build before boards did.
func guardBand(b board.BassBand) outchain.GuardBand {
	if b.CrossoverHz == 0 {
		return outchain.DefaultGuardBand
	}
	return outchain.GuardBand{CrossoverHz: b.CrossoverHz, ThresholdDb: b.ThresholdDb}
}
