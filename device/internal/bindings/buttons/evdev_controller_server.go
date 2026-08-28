//go:build server

package buttons

import "os/exec"

// biscuit: dot (action) button and volume rocker share one evdev node
// layout. See CLAUDE.md — event2 is the volume button here, but the
// touchscreen on other boards; never renumber without checking the target.
const dotButton = "/dev/input/event1"
const volumeButton = "/dev/input/event2"

// Init stops Alexa's native button service so evdev reads see every event
// instead of racing acebutton for them.
func (e *EvDevController) Init() error {
	return exec.Command("stop", "acebutton").Run()
}
