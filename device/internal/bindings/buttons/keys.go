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
	// GapMs is the press-to-press time, carried on the RELEASE so the
	// controller decides a multi-tap burst by the user's timing rather than
	// by when the two messages arrived (#115).
	GapMs int64
}

// keyState turns one input device's key events into presses and releases.
// State is per key: one device carries two buttons on every board (action and
// mute on biscuit, action and volume on the Dot 3), and pressing one while
// holding the other must not end the first.
type keyState struct {
	keys map[buttons.ClickType]keyTimes
}

// keyTimes is a key's last press and the gap that preceded it, kept after
// release so the next press can be measured against it.
type keyTimes struct {
	down  bool
	at    time.Time
	gapMs int64
}

func newKeyState() *keyState {
	return &keyState{keys: map[buttons.ClickType]keyTimes{}}
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
	p := k.keys[c]
	switch value {
	case keyPress:
		if p.down {
			return keyEvent{}, false
		}
		gap := int64(0)
		if !p.at.IsZero() {
			gap = now.Sub(p.at).Milliseconds()
		}
		k.keys[c] = keyTimes{down: true, at: now, gapMs: gap}
		return keyEvent{Code: c, Down: true}, true
	case keyRelease:
		if !p.down {
			return keyEvent{}, false
		}
		p.down = false
		k.keys[c] = p
		return keyEvent{Code: c, HeldMs: now.Sub(p.at).Milliseconds(), GapMs: p.gapMs}, true
	}
	return keyEvent{}, false // keyRepeat, or a value evdev does not define
}

// isVolume is whether a key is one of the volume pair. What a key means is
// its code, never which device carried it.
func isVolume(c buttons.ClickType) bool {
	return c == buttons.VolumeUpClick || c == buttons.VolumeDownClick
}
