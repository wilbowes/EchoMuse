package led

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// Amazon's privacy driver, present on the FireOS 6 kernel (amz_priv.c, driven
// from kpd.c) and absent on FireOS 5's. Where it exists it owns gpio444 — the
// mute button LED, which it names amz_priv_trig — so the sysfs GPIO path above
// fails with EBUSY and the LED is the driver's to light.
//
// What the source says, confirmed on 15LE 2026-09-26:
//   - It toggles its own state on every mute-button release: entering waits
//     300ms (privacy_timer_on reads 1 meanwhile), leaving is immediate.
//   - Software can ENTER privacy (write 1 to privacy_trigger) and can never
//     leave it: "ignore exit privacy mode from software". Only the button
//     unmutes the LED.
//   - It always boots unmuted, whatever our persisted mute says.
//
// Donut/crumpet instead expose gpio-privacy/{state,enable}: a hardware latch
// that software can also enter but never clear. It has no entry timer.
// Found by driver name, never by the keypad's address.
// Never read power_button_state: it reads a mute GPIO biscuit's device tree
// does not define, and it blocked the console on read.
var (
	privacyOnce sync.Once
	privacyPath string
)

func privacyDir() string {
	privacyOnce.Do(func() {
		matches, _ := filepath.Glob("/sys/devices/soc/*/amz_privacy/privacy_state")
		if len(matches) > 0 {
			privacyPath = filepath.Dir(matches[0])
		} else if _, err := os.Stat("/sys/devices/platform/gpio-privacy/state"); err == nil {
			privacyPath = "/sys/devices/platform/gpio-privacy"
		}
	})
	return privacyPath
}

// PrivacyDriver reports whether Amazon's privacy driver owns the mute LED.
func PrivacyDriver() bool { return privacyDir() != "" }

// GPIOPrivacyDriver identifies the hardware latch on donut/crumpet. Its input
// events announce a state change; reading state determines mute versus unmute.
func GPIOPrivacyDriver() bool { return filepath.Base(privacyDir()) == "gpio-privacy" }

func readPrivacyFlag(name string) (bool, error) {
	b, err := os.ReadFile(filepath.Join(privacyDir(), name))
	if err != nil {
		return false, fmt.Errorf("privacy driver: read %s: %w", name, err)
	}
	switch strings.TrimSpace(string(b)) {
	case "0":
		return false, nil
	case "1":
		return true, nil
	default:
		return false, fmt.Errorf("privacy driver: invalid %s value", name)
	}
}

// PrivacyMuted is the driver's own mute state (and so its LED).
func PrivacyMuted() (bool, error) {
	if GPIOPrivacyDriver() {
		return readPrivacyFlag("state")
	}
	return readPrivacyFlag("privacy_state")
}

// PrivacyEntering reports a button-started entry still in its 300ms wait.
func PrivacyEntering() (bool, error) {
	if GPIOPrivacyDriver() {
		return false, nil // the latch changes before its debounced input event
	}
	return readPrivacyFlag("privacy_timer_on")
}

// EnterPrivacy puts the driver in its muted state, lighting the LED. There is
// no inverse.
func EnterPrivacy() error {
	name := "privacy_trigger"
	if GPIOPrivacyDriver() {
		name = "enable"
	}
	if err := os.WriteFile(filepath.Join(privacyDir(), name), []byte("1"), 0644); err != nil {
		return fmt.Errorf("privacy driver: write %s: %w", name, err)
	}
	return nil
}
