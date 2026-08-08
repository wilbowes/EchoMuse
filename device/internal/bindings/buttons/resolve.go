package buttons

import (
	"log"
	"os"
	"strings"
)

// Input device NAMES, which are stable, rather than event numbers, which are
// an enumeration accident.
//
// On a Dot with nothing in the jack the order is ACCDET, mtk-kpd, keys, so
// the buttons land on event1 and event2 and the old hardcoded constants were
// right. Nothing guarantees that. The MediaTek accessory-detect driver
// (`soc:accdet@`, which publishes ACCDET) probes ahead of both keypads, so if
// it probes differently with a jack already inserted, everything after it
// shifts and the reader opens the wrong device — or opens one that never
// produces the events it is waiting for.
//
// That failure is SILENT, which is what makes it worth removing rather than
// arguing about: `evdev.Open` succeeds on any of these, so a device whose
// numbering moved has working hardware, no error anywhere, and a dead action
// button. It matches issue #80, where booting with an aux cable in left a
// device with no working buttons and no wake word.
//
// Same rule as the ambient light sensor (resolved by driver name, because
// `0-0039` is an enumeration accident) and the thermal zones (resolved by
// type). See docs and CLAUDE.md.
const (
	// mtk-kpd carries BOTH the action button and mute.
	keypadDeviceName = "mtk-kpd"
	// gpio-keys, carrying volume up/down.
	volumeDeviceName = "keys"
)

const inputDevicesPath = "/proc/bus/input/devices"

// parseInputDevices maps input device name to its /dev/input/eventN path.
//
// Pure, so it is testable on the host against real captured output — the
// hardware this parses cannot be exercised by `go test`.
//
// Format is stanzas separated by blank lines:
//
//	N: Name="mtk-kpd"
//	H: Handlers=gpufreq_ib event1 dynamic_boost
//
// Handlers carries unrelated entries (`gpufreq_ib`, `dynamic_boost`) around
// the one that matters, so the event token is FOUND not indexed.
func parseInputDevices(content string) map[string]string {
	out := map[string]string{}
	name := ""
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimSpace(line)
		switch {
		case line == "":
			// Stanza boundary. Drop any name we did not pair with a handler,
			// so a nameless stanza cannot inherit the previous one's name.
			name = ""
		case strings.HasPrefix(line, "N: Name="):
			name = strings.Trim(strings.TrimPrefix(line, "N: Name="), `"`)
		case strings.HasPrefix(line, "H: Handlers=") && name != "":
			for _, f := range strings.Fields(strings.TrimPrefix(line, "H: Handlers=")) {
				if strings.HasPrefix(f, "event") {
					out[name] = "/dev/input/" + f
					break
				}
			}
		}
	}
	return out
}

// resolveInputDevice returns the event path for a named input device, falling
// back to the historical hardcoded path when the name cannot be found.
//
// The fallback is deliberate: an unreadable procfs or a differently-named
// keypad on some other SKU should leave the device behaving exactly as it did
// before this existed, not stop the buttons working. Every outcome is logged
// with what WAS present, so an absence can be diagnosed from a log line
// instead of a round trip asking someone to run commands on a device they
// have just flashed.
func resolveInputDevice(name, fallback string) string {
	b, err := os.ReadFile(inputDevicesPath)
	if err != nil {
		log.Printf("[buttons] %s unreadable (%v) — using %s", inputDevicesPath, err, fallback)
		return fallback
	}

	devices := parseInputDevices(string(b))
	if path, ok := devices[name]; ok {
		if path != fallback {
			// The case this whole file exists for. Worth its own line: it is
			// the difference between a device that works and one whose
			// buttons are silently dead.
			log.Printf("[buttons] %q is at %s, not the expected %s — enumeration has shifted", name, path, fallback)
		} else {
			log.Printf("[buttons] %q resolved to %s", name, path)
		}
		return path
	}

	seen := make([]string, 0, len(devices))
	for n := range devices {
		seen = append(seen, n)
	}
	log.Printf("[buttons] %q not found in %s (saw %v) — using %s",
		name, inputDevicesPath, seen, fallback)
	return fallback
}
