// Package platform answers which base operating system the firmware booted on.
//
// The firmware runs on two bases and will for as long as the fleet runs FireOS:
// Amazon's Android 5.1 userspace, and emOS (see emos/README.md), which replaces
// it with our own init on the same kernel. Almost nothing in the firmware needs
// to care — the hardware is reached through ALSA, i2c, evdev and sysfs either
// way, which is the whole reason emOS was possible. What DOES need to care is
// the controller: it distributes payloads that only mean something under
// Android (the debloat script, the pm-hide list) and would otherwise keep
// pushing them at a device with no package manager to hide anything from.
//
// This is a runtime VALUE, not a capability. The capability question — "can
// this firmware run on emOS" — is answered yes by every build that contains
// this file, so announcing it would tell the controller nothing. The useful
// question is which base the device actually booted, and that is the same
// "could it" vs "is it" split as aec_hw_ref against the aecRef stats field.
package platform

import (
	"os"
	"strings"
	"sync"
)

// The three answers. Unknown is not a failure: firmware older than this field
// reports nothing at all, and the controller must read that as "not asked"
// rather than as FireOS. Guessing a base is how a device gets sent payloads it
// cannot use, which is the fault this exists to prevent.
const (
	EmOS    = "emos"
	FireOS  = "fireos"
	Unknown = "unknown"
)

// Detect resolves the base OS beneath root. Split from Base for testing —
// the whole point is to exercise both platforms from a host that is neither.
//
// Order is belt and braces rather than precedence: the two markers are
// mutually exclusive in practice, since emOS has no property service and
// FireOS does not stamp our os-release.
func Detect(root string) string {
	// emOS stamps /etc/os-release at BUILD time, so the file describes the
	// image rather than something a running system can drift from. init
	// creates it in the ramdisk before building the /etc symlink farm, and
	// symlinking over an existing name simply fails — so on emOS this is
	// always our file, never Amazon's /system/etc.
	if b, err := os.ReadFile(root + "/etc/os-release"); err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			if strings.TrimSpace(line) == "ID="+EmOS {
				return EmOS
			}
		}
	}
	// Android's property service. The same marker start_server.sh gates on,
	// deliberately: one runtime test, used identically in both places, beats
	// a build flag and the two artifacts that come with it.
	if _, err := os.Stat(root + "/dev/__properties__"); err == nil {
		return FireOS
	}
	return Unknown
}

var (
	once   sync.Once
	cached string
)

// Base returns the detected base OS, resolved once. It cannot change without a
// reboot, and caching also means the value reported on the first stats tick is
// the value reported on every later one — a field that flapped would be read as
// a device changing platform underneath the controller.
func Base() string {
	once.Do(func() { cached = Detect("") })
	return cached
}
