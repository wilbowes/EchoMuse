package buttons

import (
	"time"

	"github.com/wilbowes/EchoMuse/pkg/buttons"
)

// evdev key values (Documentation/input/input.rst): 0 release, 1 press,
// 2 autorepeat while held.
const (
	keyRelease = 0
	keyPress   = 1
	keyRepeat  = 2
)

// keyEvent is one press or release of one key.
type keyEvent struct {
	Code   buttons.ClickType
	Down   bool
	HeldMs int64 // on release, how long the key was down
}

// keyState turns one input device's key events into presses and releases.
// State is per key: one device carries two buttons on every board (action and
// mute on biscuit, action and volume on the Dot 3), and pressing one while
// holding the other must not end the first.
type keyState struct {
	downAt map[buttons.ClickType]time.Time
}

func newKeyState() *keyState {
	return &keyState{downAt: map[buttons.ClickType]time.Time{}}
}

// handle returns the press or release an event makes, false for none: an
// autorepeat, a repeat of the state the key is already in, or code 0, which
// no button sends. Holding the action button is how pairing is asked for
// (7s), and the Dot 3's gpio-keys autorepeats, so a repeat read as a release
// would end every hold within a second.
func (k *keyState) handle(code uint16, value int32, now time.Time) (keyEvent, bool) {
	c := buttons.ClickType(code)
	if code == 0 {
		return keyEvent{}, false
	}
	t, held := k.downAt[c]
	switch value {
	case keyPress:
		if held {
			return keyEvent{}, false
		}
		k.downAt[c] = now
		return keyEvent{Code: c, Down: true}, true
	case keyRelease:
		if !held {
			return keyEvent{}, false
		}
		delete(k.downAt, c)
		return keyEvent{Code: c, HeldMs: now.Sub(t).Milliseconds()}, true
	}
	return keyEvent{}, false // keyRepeat, or a value evdev does not define
}

// isVolume is whether a key is one of the volume pair. What a key means is
// its code, never which device carried it.
func isVolume(c buttons.ClickType) bool {
	return c == buttons.VolumeUpClick || c == buttons.VolumeDownClick
}
