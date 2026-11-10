package buttons

import (
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/buttons"
)

// From the evdev spec's values (0 release, 1 press, 2 autorepeat), in the
// sequences a held key produces.

func TestAHeldKeyIsOnePressAndOneRelease(t *testing.T) {
	k := newKeyState()
	t0 := time.Unix(0, 0)
	var got []keyEvent
	for i, v := range []int32{keyPress, keyRepeat, keyRepeat, keyRepeat, keyRelease} {
		if ev, ok := k.handle(uint16(buttons.DotClick), v, t0.Add(time.Duration(i)*time.Second)); ok {
			got = append(got, ev)
		}
	}
	if len(got) != 2 || !got[0].Down || got[1].Down || got[1].HeldMs != 4000 {
		t.Fatalf("got %+v, want a press then a release held 4000ms", got)
	}
}

// One device carries two buttons; holding one while tapping the other must
// leave the first held, with its own hold time.
func TestKeysOnOneDeviceAreTrackedApart(t *testing.T) {
	k := newKeyState()
	t0 := time.Unix(0, 0)
	steps := []struct {
		code  buttons.ClickType
		value int32
		at    time.Duration
	}{
		{buttons.DotClick, keyPress, 0},
		{buttons.VolumeUpClick, keyPress, time.Second},
		{buttons.VolumeUpClick, keyRelease, 2 * time.Second},
		{buttons.DotClick, keyRelease, 7 * time.Second},
	}
	var got []keyEvent
	for _, s := range steps {
		if ev, ok := k.handle(uint16(s.code), s.value, t0.Add(s.at)); ok {
			got = append(got, ev)
		}
	}
	if len(got) != 4 || got[2].Code != buttons.VolumeUpClick || got[2].HeldMs != 1000 ||
		got[3].Code != buttons.DotClick || got[3].HeldMs != 7000 {
		t.Fatalf("got %+v", got)
	}
}

// A release with no press (a key already down when we opened the device), a
// second press, code 0, and values evdev does not define are not events.
func TestNothingButATransitionIsAnEvent(t *testing.T) {
	k := newKeyState()
	now := time.Unix(0, 0)
	for _, c := range []struct {
		code  uint16
		value int32
	}{
		{uint16(buttons.MuteClick), keyRelease},
		{uint16(buttons.MuteClick), keyRepeat},
		{0, keyPress},
		{uint16(buttons.MuteClick), 3},
		{uint16(buttons.MuteClick), -1},
	} {
		if ev, ok := k.handle(c.code, c.value, now); ok {
			t.Errorf("code %d value %d made %+v", c.code, c.value, ev)
		}
	}
	if _, ok := k.handle(uint16(buttons.MuteClick), keyPress, now); !ok {
		t.Fatal("control: a press must be an event")
	}
	if _, ok := k.handle(uint16(buttons.MuteClick), keyPress, now); ok {
		t.Error("a second press without a release is not an event")
	}
}

func TestOnlyTheVolumePairIsVolume(t *testing.T) {
	for c, want := range map[buttons.ClickType]bool{
		buttons.VolumeUpClick: true, buttons.VolumeDownClick: true,
		buttons.DotClick: false, buttons.MuteClick: false,
	} {
		if isVolume(c) != want {
			t.Errorf("isVolume(%d) = %v", c, !want)
		}
	}
}
