// Package als reads the Echo Dot's ambient light sensor.
//
// Our Dots carry an ams TSL2540 on i2c, which Amazon's Android layer does not
// expose AT ALL: `dumpsys sensorservice` reports an empty sensor list, there
// is nothing under /sys/class/sensors, and no ALS input device. It is visible
// only on the raw i2c bus — the same shape as the mute LED sitting on a
// different GPIO than the vendor HAL believed.
//
// Verified on hardware: covering the sensor by hand takes it from 309 lux to
// 0 and back to 308 within a second, with both raw channels tracking.
//
// THE BUS LISTING IS NOT A HARDWARE INVENTORY. Both ALS names are registered
// by Amazon's board file, so /sys/bus/i2c/devices/ shows a tsl2540 at 0x39
// and a tsl2584tsv at 0x29 on EVERY unit regardless of what is soldered on
// (`modalias` reads `i2c:tsl2540` — static kernel data, not a chip talking).
// Which one actually answers differs by production batch, and reading the
// listing as an inventory produced a confident wrong diagnosis on #90.
//
// The boot log is the real inventory, because both drivers probe on every
// unit and record what replied. On ours:
//
//	tsl258x 0-0029: i2c_smbus_write_bytes() to cmd reg failed in taos_probe(), err = -6
//	tsl2540 0-0039: tsl2540_probe: device id:e4 ... 'tsl2540 rev. 0x61' detected
//	tsl2540 0-0039: Probe ok.
//
// So the 2584 is NOT FITTED here (-6 is ENXIO — nothing acknowledges at
// 0x29), and on the G090LF096 batch it is the 2584 that is present and the
// 2540 that goes unanswered, surfacing through IIO as
// /sys/bus/iio/devices/iio:device0 instead of on the i2c bus. That is a
// second-sourced part, not a driver fault: matching `tsl2540` specifically is
// right for the hardware it names, and reaching the other batch means reading
// the IIO sensor too, not loosening this match to a "tsl" prefix.
//
// Do NOT try to force the driver on via /sys/bus/i2c/drivers/tsl2540/unbind.
// It succeeds and leaves every als_* attribute in place; the next read enters
// a show() handler whose driver data is gone and hangs the device hard enough
// to need a power cycle. Attribute presence does not prove a driver is bound.
//
// This is its own package because two callers need it at different moments:
// the register message needs to know whether the sensor EXISTS, to declare
// the capability; the stats tick needs to READ it.
package als

import (
	"context"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// driverName is the sysfs `name` of the sensor that has a usable interface.
const driverName = "tsl2540"

// RetryInterval bounds how often an unresolved sensor is looked for again.
// The scan is a glob plus a handful of small sysfs reads, so this is about
// not doing it every second for the life of a device that genuinely has no
// sensor, not about the cost of any single scan.
const RetryInterval = 30 * time.Second

// i2cGlob is where the bus is enumerated. A variable only so the tests can
// point it at a fixture directory — nothing reassigns it at runtime.
var i2cGlob = "/sys/bus/i2c/devices/*/name"

// Status codes. Stable identifiers, because the controller and dashboard key
// off them; the human-readable part rides in Detail.
const (
	// StatusOK — sensor found and readable.
	StatusOK = "ok"
	// StatusNoChip — the bus does not even list a tsl2540. Since the name
	// is registered by the board file on every unit, this means an
	// unfamiliar kernel rather than a missing part, and has not been seen
	// in the field.
	StatusNoChip = "no_chip"
	// StatusNoAttribute — the name is listed but no als_lux appeared, so the
	// driver's probe found nothing to talk to. This is the ordinary reading
	// for a batch that was fitted the OTHER ALS (#90) — the Detail text
	// calls it an unbound driver, which reads as our fault and is not; it is
	// corrected alongside the IIO fallback rather than on its own, so the
	// wording changes once, with the behaviour it describes.
	StatusNoAttribute = "no_attribute"
	// StatusUnknown — the bus could not be enumerated. Distinct from
	// "nothing found", which is a positive result.
	StatusUnknown = "unknown"
)

// Status is why the sensor is or is not available.
//
// This exists because the failure was previously only ever LOGGED, on the
// device, to a file that a reboot clears and that support bundles do not
// collect (issue #90). Two users with no sensor could not be told apart from
// each other, or from a device whose driver simply had not bound, without a
// shell session on their own hardware. The reason is a hardware fact the
// device already knows at registration — so it should be reported, not left
// for someone to go and look for.
type Status struct {
	Code string `json:"code"`
	// Detail is a sentence for a human reading a support bundle.
	Detail string `json:"detail,omitempty"`
	// Seen is every i2c device name on the bus, which is what makes a
	// no_chip answer verifiable rather than merely asserted — and what
	// identifies an unfamiliar revision the first time one appears.
	Seen []string `json:"seen,omitempty"`
	// Path is where the sensor was found, when it was.
	Path string `json:"path,omitempty"`
}

var (
	mu       sync.Mutex
	path     string    // absolute path to als_lux; empty when unresolved
	lastScan time.Time // when we last looked, for the retry interval
	reported bool      // absence logged once, not every retry
	status   = Status{Code: StatusUnknown, Detail: "i2c bus not scanned yet"}
)

// resolve finds the sensor BY NAME, not by i2c address.
//
// 0-0039 is where it sits on every device measured, but an address is an
// enumeration accident: hardcoding it would work here and silently read
// nothing on a device that enumerated differently. Same reasoning as
// resolving thermal zones by type rather than by index.
//
// A NEGATIVE RESULT IS NOT CACHED. This used to be a sync.Once, which meant
// one failed lookup disabled the sensor for the entire life of the process —
// and the first lookup happens at registration, moments after a cold boot,
// which is precisely when sysfs is least likely to be complete. The device
// then reported no `ambient_light` capability until something restarted it,
// with nothing in the log to say the answer had been frozen. Absence must
// stay re-checkable; a device that gains the sensor picks it up on the next
// scan and declares the capability at its next registration.
func resolve() string {
	mu.Lock()
	defer mu.Unlock()
	if path != "" {
		return path
	}
	if !lastScan.IsZero() && time.Since(lastScan) < RetryInterval {
		return ""
	}
	lastScan = time.Now()

	names, err := filepath.Glob(i2cGlob)
	if err != nil {
		status = Status{Code: StatusUnknown, Detail: "could not enumerate the i2c bus"}
		return ""
	}
	// Record what IS on the bus, so an absence can be diagnosed remotely
	// from a log line instead of a round trip asking someone to run i2c
	// commands on a device they just flashed.
	//
	// The WHOLE bus is enumerated before matching, rather than stopping at
	// the sensor. Returning early left `Seen` truncated at whatever happened
	// to sort before tsl2540 — and comparing a working device's bus against
	// a broken one's is the entire point of the field, so a partial list from
	// the healthy side defeats it. Caught on hardware; the fixtures could not
	// see it because none of them had devices sorting after the match.
	seen := make([]string, 0, len(names))
	nameMatched := false
	found := ""
	for _, n := range names {
		b, err := os.ReadFile(n)
		if err != nil {
			continue
		}
		got := strings.TrimSpace(string(b))
		seen = append(seen, got)
		if got != driverName {
			continue
		}
		nameMatched = true
		p := filepath.Join(filepath.Dir(n), "als_lux")
		// A matching name is not enough: the name is board-file data and
		// is present whether or not the chip is, so als_lux existing is
		// what separates a fitted sensor from a declared one.
		if _, err := os.Stat(p); err != nil {
			continue
		}
		if found == "" {
			found = p
		}
	}
	if found != "" {
		path = found
		status = Status{Code: StatusOK, Path: found, Seen: seen}
		log.Printf("[als] ambient light sensor at %s", path)
		return path
	}
	// Record the verdict on EVERY scan, not only the first. The log line
	// below is deliberately once-only, but a status that went stale would
	// report an old answer for a device whose bus has since changed.
	if nameMatched {
		status = Status{
			Code:   StatusNoAttribute,
			Detail: driverName + " is on the i2c bus but exposes no als_lux attribute — the driver has not bound",
			Seen:   seen,
		}
	} else {
		status = Status{
			Code:   StatusNoChip,
			Detail: "no " + driverName + " on the i2c bus — this hardware revision appears not to have the sensor fitted",
			Seen:   seen,
		}
	}
	if !reported {
		reported = true
		// The two failures need opposite fixes and look identical from the
		// controller, so name which one it is: no chip on the bus at all,
		// versus the chip present with no driver attribute bound to it.
		if nameMatched {
			log.Printf("[als] %s found but no als_lux attribute — driver not bound; "+
				"ambient light unavailable", driverName)
		} else {
			log.Printf("[als] no %s on i2c (saw: %s) — ambient light unavailable, "+
				"rechecking every %s", driverName, strings.Join(seen, ","), RetryInterval)
		}
	}
	return ""
}

// Present reports whether this device has a readable ambient light sensor.
//
// Used to decide whether to declare the capability at registration, so the
// controller never advertises an HA entity that could not produce a reading.
func Present() bool { return resolve() != "" }

// Report returns why the sensor is or is not available, resolving first so the
// answer reflects the bus as it is now rather than as it was at boot.
//
// Sent with the register message: a device that declares no ambient_light
// capability should be able to say WHY, so the answer reaches a support bundle
// instead of living in a log file on the user's device (#90).
func Report() Status {
	resolve()
	mu.Lock()
	defer mu.Unlock()
	return status
}

// Lux returns the ambient light level, or nil when there is no sensor or the
// read fails.
//
// nil, never 0: a covered sensor reads a genuine 0 lux, so reporting 0 for
// "absent" would make a dark room and a device without the hardware
// indistinguishable — the same NULL-not-zero rule the playback and shadow
// counters follow.
func Lux() *int {
	p := resolve()
	if p == "" {
		return nil
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return nil
	}
	n, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil {
		return nil
	}
	return &n
}

// ── Change watching ──────────────────────────────────────────────────────────
//
// The stats tick reports lux every ~30s, which is fine for a baseline and
// useless for "someone turned a light on": up to 30s late, and a brief change
// can fall entirely between samples.
//
// So the same split shadow mode uses for wake-word crossings — a significant
// change goes IMMEDIATELY because its whole value is the timing, and the
// steady state rides the existing summary. Nothing per-frame either way.
const (
	// PollInterval bounds how fast a change can be noticed. Reading faster
	// than the chip's 346ms integration time buys nothing but syscalls.
	//
	// 5s, not 1s, and the reason is the KERNEL LOG rather than the syscalls.
	// This driver prints a line on every read that lands under its darkness
	// threshold:
	//
	//     tsl2540 0-0039: tsl2540_get_lux: darkness (0 <= 10)
	//
	// At 1Hz that is ~86,000 kernel log lines a day, and it only fires in the
	// dark — so it runs all night, which is precisely when a device sits idle
	// and a crash most needs explaining afterwards. Measured on EFF
	// 2026-09-04: the whole log ring was this one line, and
	// /data/emos/messages.last had reached 609KB of it.
	//
	// The cost is not the disk. It is that MediaTek's ram_console is the ONLY
	// crash channel this kernel has — /proc/last_kmsg is what root-caused the
	// C95 kernel panic — and it is a fixed-size ring we do not control the
	// content of. Anything that fills it evicts the evidence, and no amount of
	// care on our own logging can offset that.
	//
	// 5s cuts it by 80% and costs nothing anybody can perceive: MinInterval
	// already refuses to report more often than every 2s, so a 1s poll was
	// finer than the reporting floor to begin with. If ambient-driven ring
	// brightness (#296) ever wants faster, make the poll adaptive — quick
	// while the level is moving, slow while it is not — rather than paying a
	// permanent log flood for a latency nobody is watching for.
	PollInterval = 5 * time.Second

	// MinRatio is how much the level must change, RELATIVE to the level it
	// changed from. An absolute threshold cannot work across the range: 50
	// lux is a transformation in a dark room and invisible in daylight, and
	// perceived brightness is roughly logarithmic anyway.
	//
	// Measured noise on a still room is about ±1.5% (309/311/313/308/312 on
	// consecutive reads), so 25% is far clear of it while still catching a
	// lamp being switched on.
	MinRatio = 0.25

	// MinAbsolute stops near-darkness generating infinite ratios — 0 -> 2 lux
	// is a 2x change and not a room lighting up.
	MinAbsolute = 10

	// MinInterval keeps a flickering or dimming light from flooding the
	// control plane. A real lighting change is a step, not a stream.
	MinInterval = 2 * time.Second
)

// Significant reports whether `now` differs enough from `baseline` to be worth
// telling anyone about. Pure, so the threshold policy is testable without a
// sensor.
func Significant(baseline, now int) bool {
	d := now - baseline
	if d < 0 {
		d = -d
	}
	if d < MinAbsolute {
		return false
	}
	ref := baseline
	if ref < MinAbsolute {
		ref = MinAbsolute // near zero, compare against the floor not against 0
	}
	return float64(d)/float64(ref) >= MinRatio
}

// Watch polls the sensor and calls onChange when the level moves significantly.
//
// onChange is called from this goroutine and must not block: it is a control
// -plane send, and a stalled send must not wedge the watcher into reporting a
// stale baseline forever.
func Watch(ctx context.Context, onChange func(lux int)) {
	if !Present() {
		return
	}
	baseline := -1
	last := time.Time{}
	t := time.NewTicker(PollInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			p := Lux()
			if p == nil {
				continue
			}
			v := *p
			if baseline < 0 {
				baseline = v // first reading seeds, never reports
				continue
			}
			if !Significant(baseline, v) {
				continue
			}
			if time.Since(last) < MinInterval {
				continue
			}
			baseline, last = v, time.Now()
			onChange(v)
		}
	}
}
