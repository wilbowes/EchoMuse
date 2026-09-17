// Package board identifies the hardware the firmware is running on and holds
// what differs per board as data (#541).
//
// Detection must be POSITIVE. An unrecognised device gets no board, and a nil
// board means "touch nothing": the kernel defaults stay in force, which for
// thermal policy are stricter than any profile here. Writing one board's
// thermal profile onto another is the failure this package exists to prevent.
//
// The device tree cannot identify a board on these kernels — every MT8163
// product reports model "MT8163" — so identity comes from Amazon's idme,
// which carries the product's device type id.
package board

import (
	"os"
	"path/filepath"
	"strings"
)

// Board is one supported piece of hardware.
type Board struct {
	// ID is the stable name reported to the controller.
	ID string
	// DeviceTypeID is Amazon's product id from /proc/idme/device_type_id.
	// Matched exactly.
	DeviceTypeID string
	// Tuning is the platform policy applied at boot on emOS. Nil means none.
	Tuning *Tuning
}

// Biscuit is the Echo Dot 2nd gen (MT8163). The same id was read off devices
// on the FireOS 5 and FireOS 6 kernels, under both FireOS and emOS.
var Biscuit = &Board{
	ID:           "biscuit",
	DeviceTypeID: "A3S5BH2HU6VAYF",
	Tuning:       biscuitTuning,
}

// Known is every board the firmware can identify.
var Known = []*Board{Biscuit}

// Detect returns the board beneath root, or nil when none matches. root is ""
// on a device and a fixture directory in tests.
func Detect(root string) *Board {
	b, err := os.ReadFile(filepath.Join(root, "/proc/idme/device_type_id"))
	if err != nil {
		return nil
	}
	// idme values are NUL-terminated (15 bytes for a 14-character id).
	if i := strings.IndexByte(string(b), 0); i >= 0 {
		b = b[:i]
	}
	id := strings.TrimSpace(string(b))
	if id == "" {
		return nil
	}
	for _, k := range Known {
		if k.DeviceTypeID == id {
			return k
		}
	}
	return nil
}

// IDOf is the board id for reporting, "unknown" when nil.
func IDOf(b *Board) string {
	if b == nil {
		return "unknown"
	}
	return b.ID
}
