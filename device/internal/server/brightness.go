package server

import (
	"math"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// ledBrightnessDefault is "as bright as the ring has always been": a device
// nothing has configured renders exactly the pre-#295 frames. It lives in the
// render path rather than in config so no frame depends on the controller
// having connected (or having a stored value).
const ledBrightnessDefault = 100

// ringGamma is the exponent between the brightness percent and the frame
// multiplier. Same reasoning as meterDefaults.gamma: an LED's output is not
// linear in perceived brightness, so painting a frame at 0.5 does not look
// half as bright — the eye reads roughly its 1/2.2 power, ~0.73, and the
// ring barely dims until the value is already low. Raising the proportion to
// 2.2 makes the number on the slider the number the eye sees. It matches the
// meter curve so a dimmed ring and a dimmed meter agree.
const ringGamma = 2.2

// ringScale is the frame multiplier for a brightness percent (0-100). Pure,
// so the curve is testable without hardware.
func ringScale(percent float64) float64 {
	if percent <= 0 {
		return 0
	}
	if percent >= 100 {
		return 1
	}
	return math.Pow(percent/100, ringGamma)
}

// brightnessController dims every frame written through it. It is the whole
// "applied at render" mechanism (#295): the ring painters hand their frames
// to this wrapper instead of the driver, so one value dims all of them — the
// controller's set_leds frames, the listening ring, the animations, the
// volume arc, the playback meter, the link pulse — without any of them
// knowing the setting exists.
//
// The red MUTE ring deliberately does NOT come through here (mute.go holds
// the raw controller). Red is how the device says the mic is muted, it is
// device-sovereign, and it has to read from across a room: a display setting
// that can hide it is a privacy fault, not a dim ring. Exempting it rather
// than flooring the brightness for it is the difference between "you cannot
// turn the ring off" and "you cannot turn the ring below 5%" — the second
// still leaves the one state that must never be missed nearly invisible.
type brightnessController struct {
	led.Controller
	percent func() float64
}

func (b brightnessController) SetLEDs(leds ...led.Led) error {
	return b.Controller.SetLEDs(scaleFrame(leds, ringScale(b.percent()))...)
}

// ringLEDs returns the controller the ring painters should use: the driver,
// dimmed by the current ledBrightness. Nil while the driver is still coming
// up, which every caller already handles.
func (s *Server) ringLEDs() led.Controller {
	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()
	if lc == nil {
		return nil
	}
	return brightnessController{Controller: lc, percent: s.ledBrightnessPercent}
}

// ledBrightnessPercent is the current setting as a float for the render
// curve. Nil (nothing has set it) means the default — a zero-value Server
// renders at full brightness rather than dark — because 0 itself is a
// legitimate setting and cannot double as "unset".
func (s *Server) ledBrightnessPercent() float64 {
	p := s.ledBrightness.Load()
	if p == nil {
		return ledBrightnessDefault
	}
	return float64(*p)
}

// SetLEDBrightness applies the ledBrightness config (#295). Repaints at once:
// the value arrives from a slider being dragged, and a device that only
// changed on its next frame (up to the 2s volume window, or never on a static
// ring) reads as a dead control.
func (s *Server) SetLEDBrightness(percent int) {
	if percent < 0 {
		percent = 0
	}
	if percent > 100 {
		percent = 100
	}
	if p := s.ledBrightness.Load(); p != nil && *p == percent {
		return
	}
	s.ledBrightness.Store(&percent)
	// Nothing to repaint through a held ring: the flash and the volume arc
	// are transient, the animations repaint themselves within a frame, and
	// the mute ring is exempt from the setting altogether.
	if s.ringHeld() {
		return
	}
	s.paintBaseLEDs()
}

// ringHeld reports whether another subsystem currently owns the ring, in
// which case a paint from outside it must not go through. Shared by SetLEDs
// and SetLEDBrightness so the two cannot disagree about who owns the ring.
func (s *Server) ringHeld() bool {
	return s.flashActive() ||
		suppressPaint(s.volume.DisplayActive(), s.mute.IsMuted(), s.LinkDown())
}
