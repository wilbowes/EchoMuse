package led

import (
	"fmt"
	"os"
)

// Mute-button LED — the discrete red LED under the mic-off button, separate
// from the 12-LED ring. Stock FireOS drives it through GPIO 445 via sysfs
// (libled_hal.so: IssiLedDevice::exportMuteButtonGPIO / k_muteButtonGPIOAddress
// = 0x1BD = 445, read straight out of the ELF 2026-07-12). The line is
// ACTIVE-LOW: disassembly of setMuteButtonBrightness shows it streaming 0
// to the value file for brightness > 47 (LED on) and 1 for brightness ≤ 36
// (LED off) — get this backwards and the button glows whenever the device
// is unmuted. It's a plain on/off GPIO, not a PWM channel, so "brightness"
// is binary. The stock ledcontroller service exports it at boot before
// EchoMuse stops that service, but export is re-done here defensively in
// case boot ordering ever changes.
const (
	muteButtonGPIO      = "445"
	gpioExportPath      = "/sys/class/gpio/export"
	muteButtonDirPath   = "/sys/class/gpio/gpio" + muteButtonGPIO + "/direction"
	muteButtonValuePath = "/sys/class/gpio/gpio" + muteButtonGPIO + "/value"
)

// InitMuteButtonLED exports the GPIO if needed, forces output direction,
// and switches the LED off (the process starts unmuted; a crash while
// muted must not leave a stale red button on restart).
func InitMuteButtonLED() error {
	if _, err := os.Stat(muteButtonValuePath); os.IsNotExist(err) {
		if err := os.WriteFile(gpioExportPath, []byte(muteButtonGPIO), 0644); err != nil {
			return fmt.Errorf("mute button LED: export gpio%s: %w", muteButtonGPIO, err)
		}
	}
	if err := os.WriteFile(muteButtonDirPath, []byte("out"), 0644); err != nil {
		return fmt.Errorf("mute button LED: set direction: %w", err)
	}
	return SetMuteButtonLED(false)
}

// SetMuteButtonLED switches the red LED under the mic-off button.
// Active-low (see package comment): 0 = on, 1 = off.
func SetMuteButtonLED(on bool) error {
	v := []byte("1")
	if on {
		v = []byte("0")
	}
	if err := os.WriteFile(muteButtonValuePath, v, 0644); err != nil {
		return fmt.Errorf("mute button LED: write value: %w", err)
	}
	return nil
}
