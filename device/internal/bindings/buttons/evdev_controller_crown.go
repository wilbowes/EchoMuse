//go:build crown

package buttons

import (
	"log"
	"path/filepath"

	evdev "github.com/gvalkov/golang-evdev"
)

// crown: live-confirmed with `getevent -lt` 2026-08-26
// (docs/echo-show-8-hardware-map.md). No separate action button — the
// mic/action button rides the "gating" driver as KEY_POWER (code 116), not
// gpio-keys, and there is no code-138 "dot" event as on biscuit. Volume
// shares gpio-keys with the camera shutter switch, same evdev node.
//
// KNOWN GAP: KEY_POWER (116) does not match pkg/buttons.MuteClick (113), so
// SubscribeToButton's mute intercept (evdev_controller.go) does not fire for
// it yet — a press currently surfaces as an ordinary ButtonClickEvent rather
// than toggling mute, even though "firmware toggles mute in software" is the
// intended behaviour per the hardware map. Deferred rather than guessed at:
// fixing it means either mapping 116 onto MuteClick for this board or giving
// the dot device its own click-type table, and that's a UX decision, not a
// wiring one.

// Resolved BY NAME at Init(), not hardcoded by number — the root CLAUDE.md
// states this rule from two prior scars on this exact project (`event2` is
// the volume button on biscuit and the touchscreen on checkers; opening the
// wrong one succeeds silently and leaves the buttons dead), and this board's
// own discovery notes independently flagged the same numbering-collision
// risk here. `event0`/`event6` are what a live `getevent -lt` measured on
// this unit — kept as the fallback if resolution ever fails, since a wrong
// guess costs a Retry-worthy warning, and refusing to open anything at all
// costs a dead button with no recourse.
var dotButton = "/dev/input/event0"
var volumeButton = "/dev/input/event6"

const dotButtonDriver = "gating"
const volumeButtonDriver = "gpio-keys"

// resolveEvdevByName scans /dev/input/event* and returns the path whose
// EVIOCGNAME matches driverName, or "" if none does. Every candidate is
// opened and closed rather than stopping at the first match — this board
// has more input nodes than just these two (touchscreen, etc.), and only a
// full scan can tell "not found" apart from "found the wrong one first".
func resolveEvdevByName(driverName string) string {
	candidates, err := filepath.Glob("/dev/input/event*")
	if err != nil {
		return ""
	}
	for _, path := range candidates {
		dev, err := evdev.Open(path)
		if err != nil {
			continue
		}
		name := dev.Name
		dev.File.Close()
		if name == driverName {
			return path
		}
	}
	return ""
}

// Init: no native button service to stop. `stop acebutton` (biscuit's
// equivalent) returned "exit status 1" against a real crown unit — there is
// no such service on LineageOS — so this is a no-op beyond the name
// resolution above.
func (e *EvDevController) Init() error {
	if p := resolveEvdevByName(dotButtonDriver); p != "" {
		dotButton = p
	} else {
		log.Printf("[buttons] could not resolve %q by name, falling back to %s — "+
			"a numbering change on this device would leave the button dead", dotButtonDriver, dotButton)
	}
	if p := resolveEvdevByName(volumeButtonDriver); p != "" {
		volumeButton = p
	} else {
		log.Printf("[buttons] could not resolve %q by name, falling back to %s — "+
			"a numbering change on this device would leave volume dead", volumeButtonDriver, volumeButton)
	}
	return nil
}
