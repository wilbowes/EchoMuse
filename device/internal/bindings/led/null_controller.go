package led

import (
	"log"

	pkgled "github.com/wilbowes/EchoMuse/pkg/led"
)

// NullController satisfies the LED interface on devices with no LED ring.
//
// The Echo Show 5 has a screen where the Dot has twelve RGB LEDs, and none of
// the IS31FL3236A sysfs paths the I2C controller pokes exist. Reporting zero
// LEDs lets the animation layer and the control plane run unmodified: they
// drive an empty ring, rather than requiring every call site to learn about
// devices without one.
//
// If a visual indicator is wanted later, this is the seam: implement SetLEDs
// against an on-screen overlay and the existing state machine drives it as-is.
type NullController struct {
	logged bool
}

var _ pkgled.Controller = (*NullController)(nil)

// NewNullController returns an LED backend that discards all writes.
func NewNullController() *NullController { return &NullController{} }

// Init is a no-op; there is no hardware to bring up.
func (n *NullController) Init() error {
	log.Printf("led: no LED ring on this device, using null controller")
	return nil
}

// GetNumLEDs reports zero, so animations iterate over an empty ring.
func (n *NullController) GetNumLEDs() (int, error) { return 0, nil }

// SetLEDs discards the frame.
func (n *NullController) SetLEDs(_ ...pkgled.Led) error { return nil }
