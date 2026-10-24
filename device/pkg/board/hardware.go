package board

import (
	"fmt"
	"sync"
)

// Hardware says where a board's parts are, by NAME (#541). The numbers the
// kernel hands out are enumeration order: event2 is the volume keys on
// biscuit and something else on every other board measured, and opening the
// wrong one succeeds and reads nothing.
//
// Each part also carries the number it has had on every unit of this board
// measured so far. It is used only when the name is not found, and says so in
// the log: a kernel build that names a part differently then behaves as the
// firmware always has, and tells us. A part with no fallback is not opened by
// number at all.
type Hardware struct {
	// Keys are the input devices the buttons arrive on, by the names
	// /proc/bus/input/devices gives them. What a key means is its code, not
	// the device it came from: biscuit splits action+mute from volume, the
	// Dot 3 splits action+volume from mute.
	Keys []Input
	// LEDRing is the ring driver's i2c client, by its sysfs `name`.
	LEDRing I2C
	// MuteLEDGPIO is the sysfs GPIO number of the LED under the mute button,
	// "" when the board has none we may drive. The same number is a
	// different SoC pin on a board with another gpiochip base, so this is
	// never assumed for a board that has not stated it.
	MuteLEDGPIO string
	// Capture is the mic array's PCM and Playback the speaker's, by the id
	// /proc/asound/pcm lists. A Capture with no name is a board whose mics
	// the firmware cannot read yet: it runs without them, button-only.
	Capture  PCM
	Playback PCM
	// Routes are the codec switches that must be closed for audio to flow,
	// and InputGains the mic input levels, written once before either PCM
	// opens (internal/bindings/codec).
	Routes     []Ctl
	InputGains []Ctl
	// LightSensors are the ambient light sensors the board may carry, in the
	// order to try them; empty means the board has none. More than one when
	// the part is second-sourced (#90).
	LightSensors []LightSensor
	// HCI is the Bluetooth controller's raw HCI character device, "" when
	// the board has none the firmware can drive. A node of the same name
	// can exist on a board whose radio is not behind it, so this is stated
	// per board and never assumed.
	HCI string
	// Speaker is what the playback path needs beyond the codec routes, amp
	// switch and jack table every board shares.
	Speaker Speaker
}

// Speaker is a board's playback path where it differs between boards.
type Speaker struct {
	// AmpSwitch turns the speaker amplifier on, "" when the board has none
	// to switch.
	AmpSwitch string
	// Volume is the hardware volume the speaker sits at unity on while
	// audio is live (the user's level is applied in software), and mutes
	// with around stream and amp changes.
	Volume Volume
	// Jack is what each plug position writes; the speaker reconciles the
	// same controls against Android's HAL rewriting them.
	Jack Jack
	// AmpMute is a mixer switch that silences the amplifier while On. The
	// codec driver sets it at every stream open and close, so the speaker
	// releases it once its stream runs. "" when the board has none: biscuit
	// has a control of the same name that drives nothing.
	AmpMute string
	// Filter is the codec's DSP filter, loaded from the device's own audio
	// config. Nil when the board needs none.
	Filter *CodecFilter
	// Setup is written once, before the stream opens.
	Setup []Ctl
	// BassGuard is the band the bass guard protects on this driver.
	BassGuard BassBand
	// Voicing is nil until the board's driver has been measured.
	Voicing *Voicing
}

// Volume is a hardware volume control and its silent and 0dB values.
type Volume struct {
	Control     string
	Mute, Unity string
}

// Jack is the mixer writes for a plug going in and coming out, in order. The
// speaker's amplifier goes off first on Insert and on last on Remove, so
// nothing is switched under a playing speaker.
type Jack struct {
	Insert, Remove []Ctl
}

// CodecFilter is a byte control whose values are read from the audio HAL's
// XML config on the device's own /system, never compiled in: FireOS's HAL
// loads it, emOS has none, and the codec resets to all zeros, which is
// silence (#535).
type CodecFilter struct {
	Control string // the byte control
	Bytes   int    // its size
	File    string // the HAL's config
	// Speaker and Jack are the paths whose "turnon" carries the filter for
	// each output. Jack "" keeps the speaker's filter with a plug in.
	Speaker, Jack string
}

// Ctl is one mixer write, by control name.
type Ctl struct {
	Name   string
	Values []string
}

// BassBand is band 1 of the stock MBCL.cfg on a board's own driver: what it
// removes, and from what level.
type BassBand struct {
	CrossoverHz float64
	ThresholdDb float64
}

// LightSensor is an ambient light sensor, found by its sysfs `name` on a bus.
type LightSensor struct {
	Driver string // the device's sysfs `name`
	Attr   string // the attribute, in the device's directory, that reads lux
	Bus    string // BusI2C or BusIIO
}

// Buses a light sensor is enumerated on.
const (
	BusI2C = "i2c" // /sys/bus/i2c/devices
	BusIIO = "iio" // /sys/bus/iio/devices
)

// Input is an evdev device.
type Input struct {
	Name     string // exact device name
	Fallback string // /dev/input/eventN, "" for none
}

// I2C is an i2c client.
type I2C struct {
	Driver   string // the client's sysfs `name`
	Fallback string // sysfs directory, "" for none
}

// PCM is an ALSA PCM device.
type PCM struct {
	Name     string   // stream name: the id up to the DAI name
	Fallback *PCMAddr // nil for none
}

// PCMAddr is a PCM's card and device number.
type PCMAddr struct{ Card, Device int }

// biscuitHardware: names read off VVV (FireOS 5.5.5.4) on 2026-10-04, numbers
// as every build before this one opened them.
var biscuitHardware = &Hardware{
	Keys: []Input{
		{Name: "mtk-kpd", Fallback: "/dev/input/event1"}, // action, mute
		{Name: "keys", Fallback: "/dev/input/event2"},    // volume
	},
	LEDRing:     I2C{Driver: "is31fl3236", Fallback: "/sys/devices/soc/11007000.i2c/i2c-0/0-003f"},
	MuteLEDGPIO: "444",
	Capture:     PCM{Name: "TLV320AIC3101 Capture", Fallback: &PCMAddr{0, 24}},
	Playback:    PCM{Name: "TLV320AIC3204 Playback", Fallback: &PCMAddr{0, 23}},
	Routes:      tlvRoutes,
	InputGains:  tlvInputGains,
	// Which one is fitted differs by batch (#90). Never the 2584's
	// calibrated_lux: reading it corrupts kernel memory.
	LightSensors: []LightSensor{
		{Driver: "tsl2540", Attr: "als_lux", Bus: BusI2C},
		{Driver: "tsl2584tsv", Attr: "illuminance0_input", Bus: BusIIO},
	},
	// MediaTek's combo-chip (WMT) Bluetooth node.
	HCI: "/dev/stpbt",
	Speaker: Speaker{
		AmpSwitch: tlvAmp,
		Volume:    tlvVolume,
		Jack:      tlvJack,
		// Read off biscuit's MBCL.cfg (#229).
		BassGuard: BassBand{CrossoverHz: 115, ThresholdDb: -50},
		Voicing:   biscuitVoicing,
	},
}

// radarHardware: read off 035T (FireOS 6, NS6575) on 2026-10-23. Every name
// below and every PCM and i2c client is biscuit's, so the parts are; what
// differs is the speaker. No number fallbacks: radar has no builds from
// before boards were told apart.
var radarHardware = &Hardware{
	Keys:       []Input{{Name: "mtk-kpd"}, {Name: "keys"}},
	LEDRing:    I2C{Driver: "is31fl3236"},
	Capture:    PCM{Name: "TLV320AIC3101 Capture"},
	Playback:   PCM{Name: "TLV320AIC3204 Playback"},
	Routes:     tlvRoutes,
	InputGains: tlvInputGains,
	// Only the 2540 has been seen on radar (035T, 07MU).
	LightSensors: []LightSensor{{Driver: "tsl2540", Attr: "als_lux", Bus: BusI2C}},
	HCI:          "/dev/stpbt",
	Speaker: Speaker{
		AmpSwitch: tlvAmp,
		Volume:    tlvVolume,
		Jack:      tlvJack,
		// Measured on 035T: the driver logs amp_mute=1 at every stream
		// open and close, and the speaker is silent until this is Off.
		AmpMute: "MFP Gpio Mute",
		Filter: &CodecFilter{
			Control: "biquad coefficients",
			Bytes:   117,
			File:    "/system/etc/audio_device.xml",
			Speaker: "ext_speaker_output",
			Jack:    "ext_headphone_output",
		},
		// Both of the HAL's output paths write it.
		Setup: []Ctl{{"DRC Control", []string{"Disabled"}}},
		// Radar's own MBCL.cfg band 1: 0-70Hz, 20:1 from -25dB. Biscuit's
		// band removed the woofer's bass at any normal level.
		BassGuard: BassBand{CrossoverHz: 70, ThresholdDb: -25},
	},
}

// donutHardware: read off 41C8 (FireOS 6.5.7.5, NS6575) on 2026-10-24. An
// MT8167 board with its own audio layout: two tlv320aic3101 channels on a
// 4-channel TDM capture and a TAS2770 amplifier on DL1. No number fallbacks.
var donutHardware = &Hardware{
	// gpio-keys carries action (KEY_HELP) and volume; Amazon's privacy
	// driver delivers the mute button.
	Keys:    []Input{{Name: "gpio-keys"}, {Name: "gpio-privacy"}},
	LEDRing: I2C{Driver: "is31fl3236"},
	// The mute LED is the privacy driver's (gpio 494), lit without us, and
	// gpio444 here is SoC pin 57 in its audio function: exporting it kills
	// the amplifier until reboot (@shortgame11's port). So none is stated.
	//
	// The mics are "TDM_Capture", 4 channels of S32_LE at 16kHz, which the
	// capture path does not read yet, so no capture is stated and the board
	// runs button-only.
	Playback:   PCM{Name: "DL1_Playback"},
	Routes:     donutRoutes,
	InputGains: donutInputGains,
	// The i2c bus also lists an opt3001 at 0-0044 with no driver bound.
	// Never calibrated_lux, as on biscuit's tsl2584.
	LightSensors: []LightSensor{{Driver: "tsl2572", Attr: "in_illuminance0_input", Bus: BusIIO}},
	// /dev/stpbt exists, but the radio behind it reads differently from
	// biscuit's (@technotiger on #527), so the proxy is off until measured.
	Speaker: Speaker{
		Volume: donutVolume,
		Jack:   donutJack,
		// Stock's speaker path, written before the stream opens.
		Setup: []Ctl{
			{"Audio Amp Playback Volume", []string{"0", "0"}},
			{"LINEOUT Mux", []string{"VOICE_AMP"}},
			{"HPOUT Mux", []string{"OPEN"}},
		},
		// Biscuit's band until the Dot 3's driver is measured.
		BassGuard: BassBand{CrossoverHz: 115, ThresholdDb: -50},
	},
}

// Layout is a board's Hardware resolved on the running device: what the
// bindings open.
type Layout struct {
	// Board is nil on a device nothing identified. Its layout is then
	// biscuit's, which is what the firmware assumed of every device before
	// boards were told apart.
	Board *Board

	// Keys are the input devices found, /dev/input/eventN, in the board's
	// order; a device not found is left out and reported.
	Keys    []string
	LEDRing string // sysfs directory, "" when not found
	// MuteLEDGPIO is "" when the board has no mute LED GPIO to drive.
	MuteLEDGPIO string
	Capture     *PCMAddr // nil when not found
	Playback    *PCMAddr
	// LightSensors and HCI are the board's own statements, not resolved here:
	// the sensor is looked for by internal/bindings/als, which retries, and
	// the HCI device is opened by internal/bluetooth only when the proxy is
	// switched on.
	LightSensors []LightSensor
	HCI          string
	// Routes, InputGains and Speaker are the board's statements too, written
	// by the codec and the speaker.
	Routes, InputGains []Ctl
	Speaker            Speaker

	// Notes has one line per part saying how it was found, for the log.
	Notes []string
	// Problems is the subset of Notes for parts NOT found by name: opened by
	// their old number, or not available. Sent to the controller, because the
	// device's own log is RAM-backed and nobody reads it on a working Echo.
	Problems []string
}

// Resolve finds each part of hw beneath root ("" on a device).
func Resolve(root string, b *Board) *Layout {
	hw := biscuitHardware
	if b != nil && b.Hardware != nil {
		hw = b.Hardware
	}
	l := &Layout{Board: b, MuteLEDGPIO: hw.MuteLEDGPIO, LightSensors: hw.LightSensors, HCI: hw.HCI,
		Routes: hw.Routes, InputGains: hw.InputGains, Speaker: hw.Speaker}
	note := func(part, want, got string, err error) {
		var line string
		switch {
		case err == nil:
			l.Notes = append(l.Notes, fmt.Sprintf("%s: %q at %s", part, want, got))
			return
		case got != "":
			line = fmt.Sprintf("%s: %v; using %s as on every unit measured", part, err, got)
		default:
			line = fmt.Sprintf("%s: %v; not available", part, err)
		}
		l.Notes = append(l.Notes, line)
		l.Problems = append(l.Problems, line)
	}
	input := func(part string, in Input) string {
		p, err := InputEvent(root, in.Name)
		if err != nil {
			p = in.Fallback
		}
		note(part, in.Name, p, err)
		return p
	}
	pcm := func(part string, want PCM, capture bool) *PCMAddr {
		a, err := PCMDevice(root, want.Name, capture)
		if err != nil {
			a = want.Fallback
		}
		got := ""
		if a != nil {
			got = fmt.Sprintf("card %d device %d", a.Card, a.Device)
		}
		note(part, want.Name, got, err)
		return a
	}
	for _, k := range hw.Keys {
		if p := input("keys", k); p != "" {
			l.Keys = append(l.Keys, p)
		}
	}
	dir, err := I2CDevice(root, hw.LEDRing.Driver)
	if err != nil {
		dir = hw.LEDRing.Fallback
	}
	note("led ring", hw.LEDRing.Driver, dir, err)
	l.LEDRing = dir
	l.Capture = pcm("capture", hw.Capture, true)
	l.Playback = pcm("playback", hw.Playback, false)
	return l
}

var (
	currentOnce   sync.Once
	current       *Board
	currentLayout *Layout
)

func resolveCurrent() {
	currentOnce.Do(func() {
		current = Detect("")
		currentLayout = Resolve("", current)
	})
}

// Current is the board the firmware is running on, detected once. Nil when
// nothing identified it.
func Current() *Board {
	resolveCurrent()
	return current
}

// CurrentLayout is Current's hardware as found on this device, resolved once.
func CurrentLayout() *Layout {
	resolveCurrent()
	return currentLayout
}
