package platform

import (
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Boot-time facts about the hardware's health, reported once per registration.
// Static for the life of a boot, so they ride the register message rather
// than the stats tick (the base_os rule).

// Emmc is the flash's own wear report (JEDEC JESD84-B51 EXT_CSD) plus the
// part's identity, so a failure can be tied to a part or batch.
type Emmc struct {
	Rev    int    `json:"rev"`    // EXT_CSD_REV [192]; below 7 the next three mean nothing
	PreEOL int    `json:"preEol"` // PRE_EOL_INFO [267]: 1 normal, 2 warning (80% of reserve), 3 urgent
	LifeA  int    `json:"lifeA"`  // DEVICE_LIFE_TIME_EST_TYP_A [268]: 1-10 in 10% steps, 11 exceeded
	LifeB  int    `json:"lifeB"`  // DEVICE_LIFE_TIME_EST_TYP_B [269]
	Name   string `json:"name,omitempty"`
	Date   string `json:"date,omitempty"`
	ManfID string `json:"manfid,omitempty"`
}

// EXT_CSD byte offsets, from the JEDEC register map.
const (
	extCSDLen    = 512
	extCSDRev    = 192
	extCSDPreEOL = 267
	extCSDLifeA  = 268
	extCSDLifeB  = 269
	healthMinRev = 7 // the health fields were added in eMMC 5.0 (rev 7)
)

// ReadEmmc reads the eMMC beneath root ("" for the live system), or nil if
// there is none or it cannot be read.
//
// The device is found by TYPE, not by number: mmc0:0001 is biscuit's
// enumeration, and an SD slot on another board is an mmc device too. EXT_CSD
// is read from debugfs because this 3.18 kernel predates the sysfs
// life_time/pre_eol_info attributes (Linux 4.9); emOS mounts debugfs from 0.3.
func ReadEmmc(root string) *Emmc {
	devs, _ := filepath.Glob(filepath.Join(root, "/sys/bus/mmc/devices/*"))
	for _, dev := range devs {
		if readTrim(filepath.Join(dev, "type")) != "MMC" {
			continue
		}
		name := filepath.Base(dev) // mmc0:0001
		host, _, _ := strings.Cut(name, ":")
		raw, err := os.ReadFile(filepath.Join(root, "/sys/kernel/debug", host, name, "ext_csd"))
		if err != nil {
			return nil
		}
		e := parseExtCSD(string(raw))
		if e == nil {
			return nil
		}
		e.Name = readTrim(filepath.Join(dev, "name"))
		e.Date = readTrim(filepath.Join(dev, "date"))
		e.ManfID = readTrim(filepath.Join(dev, "manfid"))
		return e
	}
	return nil
}

var emmcCache struct {
	sync.Mutex
	at time.Time
	v  *Emmc
}

// EmmcCached is ReadEmmc on the live system, re-read once the last reading is
// older than maxAge. A failed read is cached too, so a board without debugfs
// does not retry on every stats tick.
func EmmcCached(maxAge time.Duration) *Emmc {
	emmcCache.Lock()
	defer emmcCache.Unlock()
	if emmcCache.at.IsZero() || time.Since(emmcCache.at) >= maxAge {
		emmcCache.v, emmcCache.at = ReadEmmc(""), time.Now()
	}
	return emmcCache.v
}

// parseExtCSD decodes the kernel's hex dump of the 512-byte register. A dump
// of any other length is refused rather than indexed into, since a short read
// would put the wrong bytes under the right names.
func parseExtCSD(dump string) *Emmc {
	b, err := hex.DecodeString(strings.TrimSpace(dump))
	if err != nil || len(b) != extCSDLen {
		return nil
	}
	return &Emmc{
		Rev:    int(b[extCSDRev]),
		PreEOL: int(b[extCSDPreEOL]),
		LifeA:  int(b[extCSDLifeA]),
		LifeB:  int(b[extCSDLifeB]),
	}
}

// BootID is the kernel's random id for this boot, so the controller can count
// boots rather than registrations — a device re-registers on every redial.
func BootID(root string) string {
	return readTrim(filepath.Join(root, "/proc/sys/kernel/random/boot_id"))
}

// BootReason is what the bootloader said started this boot
// (androidboot.bootreason: power_key, wdt_by_pass_pwk, kernel_panic…), or ""
// when it is not on the cmdline. On FireOS 6's 32-bit kernel it never is: the
// cmdline is cut at 1024 bytes before LK's suffix reaches it, the same
// truncation that loses androidboot.serialno. Read from the kernel rather than
// a property, so it adds no Android call site.
func BootReason(root string) string {
	b, _ := os.ReadFile(filepath.Join(root, "/proc/cmdline"))
	return cmdlineValue(string(b), "androidboot.bootreason")
}

// cmdlineTruncatedAt is the 32-bit kernel's COMMAND_LINE_SIZE. A cmdline that
// long may have been cut, and if so its LAST argument may be cut mid-value: a
// shortened reason is well formed, so it is dropped rather than reported.
const cmdlineTruncatedAt = 1023

func cmdlineValue(cmdline, key string) string {
	fields := strings.Fields(cmdline)
	for i, f := range fields {
		if k, v, ok := strings.Cut(f, "="); ok && k == key {
			if i == len(fields)-1 && len(strings.TrimRight(cmdline, "\n")) >= cmdlineTruncatedAt {
				return ""
			}
			return v
		}
	}
	return ""
}

func readTrim(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}
