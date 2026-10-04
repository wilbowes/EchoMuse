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
	// DotKeys carries the action and mute buttons; VolumeKeys the volume
	// pair. Input device names, as /proc/bus/input/devices gives them.
	DotKeys    Input
	VolumeKeys Input
	// LEDRing is the ring driver's i2c client, by its sysfs `name`.
	LEDRing I2C
	// MuteLEDGPIO is the sysfs GPIO number of the LED under the mute button,
	// "" when the board has none we may drive. The same number is a
	// different SoC pin on a board with another gpiochip base, so this is
	// never assumed for a board that has not stated it.
	MuteLEDGPIO string
	// Capture is the mic array's PCM and Playback the speaker's, by the id
	// /proc/asound/pcm lists.
	Capture  PCM
	Playback PCM
}

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
	DotKeys:     Input{Name: "mtk-kpd", Fallback: "/dev/input/event1"},
	VolumeKeys:  Input{Name: "keys", Fallback: "/dev/input/event2"},
	LEDRing:     I2C{Driver: "is31fl3236", Fallback: "/sys/devices/soc/11007000.i2c/i2c-0/0-003f"},
	MuteLEDGPIO: "444",
	Capture:     PCM{Name: "TLV320AIC3101 Capture", Fallback: &PCMAddr{0, 24}},
	Playback:    PCM{Name: "TLV320AIC3204 Playback", Fallback: &PCMAddr{0, 23}},
}

// Layout is a board's Hardware resolved on the running device: what the
// bindings open.
type Layout struct {
	// Board is nil on a device nothing identified. Its layout is then
	// biscuit's, which is what the firmware assumed of every device before
	// boards were told apart.
	Board *Board

	DotKeys    string // /dev/input/eventN, "" when not found
	VolumeKeys string
	LEDRing    string // sysfs directory, "" when not found
	// MuteLEDGPIO is "" when the board has no mute LED GPIO to drive.
	MuteLEDGPIO string
	Capture     *PCMAddr // nil when not found
	Playback    *PCMAddr

	// Notes has one line per part saying how it was found, for the log.
	Notes []string
}

// Resolve finds each part of hw beneath root ("" on a device).
func Resolve(root string, b *Board) *Layout {
	hw := biscuitHardware
	if b != nil && b.Hardware != nil {
		hw = b.Hardware
	}
	l := &Layout{Board: b, MuteLEDGPIO: hw.MuteLEDGPIO}
	note := func(part, want, got string, err error) {
		switch {
		case err == nil:
			l.Notes = append(l.Notes, fmt.Sprintf("%s: %q at %s", part, want, got))
		case got != "":
			l.Notes = append(l.Notes, fmt.Sprintf("%s: %v; using %s as on every unit measured", part, err, got))
		default:
			l.Notes = append(l.Notes, fmt.Sprintf("%s: %v; not available", part, err))
		}
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
	l.DotKeys = input("dot keys", hw.DotKeys)
	l.VolumeKeys = input("volume keys", hw.VolumeKeys)
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
