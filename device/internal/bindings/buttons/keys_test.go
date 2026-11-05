package buttons

import (
	"slices"
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

// The multi-tap window has to compare the user's press-to-press timing (#115),
// never when the events happened to be handed on. The sleeps below are the
// link's jitter: each bears no relation to its tap's simulated time, so a gap
// taken anywhere but the press would not come out as 0, 120, 260.
func TestTapGapsComeFromThePressesNotFromTheSend(t *testing.T) {
	k := newKeyState()
	t0 := time.Unix(1789, 0)
	taps := []struct {
		at     time.Duration
		hold   time.Duration
		jitter time.Duration
	}{
		{0, 60 * time.Millisecond, 5 * time.Millisecond},
		{120 * time.Millisecond, 60 * time.Millisecond, 40 * time.Millisecond},
		{380 * time.Millisecond, 40 * time.Millisecond, time.Millisecond},
	}
	var gaps []int64
	for _, tap := range taps {
		k.handle(uint16(buttons.DotClick), keyPress, t0.Add(tap.at))
		time.Sleep(tap.jitter)
		ev, ok := k.handle(uint16(buttons.DotClick), keyRelease, t0.Add(tap.at+tap.hold))
		if !ok {
			t.Fatalf("release at %v was not an event", tap.at+tap.hold)
		}
		gaps = append(gaps, ev.GapMs)
	}
	if want := []int64{0, 120, 260}; !slices.Equal(gaps, want) {
		t.Fatalf("gaps %v, want %v — a gap taken at send time would follow the "+
			"sleeps instead of the presses", gaps, want)
	}
}

// A volume press between two taps of the action button must not reset the
// action button's burst: per-key state is why holding one button does not end
// another, and the gap follows the same rule.
func TestTapGapsArePerKey(t *testing.T) {
	k := newKeyState()
	t0 := time.Unix(1789, 0)
	k.handle(uint16(buttons.DotClick), keyPress, t0)
	k.handle(uint16(buttons.DotClick), keyRelease, t0.Add(50*time.Millisecond))
	k.handle(uint16(buttons.VolumeUpClick), keyPress, t0.Add(100*time.Millisecond))
	k.handle(uint16(buttons.VolumeUpClick), keyRelease, t0.Add(150*time.Millisecond))
	k.handle(uint16(buttons.DotClick), keyPress, t0.Add(200*time.Millisecond))
	ev, ok := k.handle(uint16(buttons.DotClick), keyRelease, t0.Add(250*time.Millisecond))
	if !ok || ev.GapMs != 200 {
		t.Fatalf("gap %d (event %+v), want 200 — the volume press is not a tap "+
			"of the action button", ev.GapMs, ev)
	}
}
