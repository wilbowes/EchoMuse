package buttons

import "testing"

// Verbatim from Office (G090LF11803611NF, v2.10.0), nothing in the jack.
// Kept exactly as the device prints it, trailing spaces in the Handlers
// lines included, because that whitespace is the sort of thing a parser
// trips on and a tidied fixture would never catch.
const officeNoJack = `I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="ACCDET"
P: Phys=
S: Sysfs=/devices/virtual/input/input0
U: Uniq=
H: Handlers=gpufreq_ib event0 dynamic_boost
B: PROP=0
B: EV=3
B: KEY=0

I: Bus=0019 Vendor=2454 Product=6500 Version=0010
N: Name="mtk-kpd"
P: Phys=
S: Sysfs=/devices/soc/10010000.keypad/input/input1
U: Uniq=
H: Handlers=gpufreq_ib event1 dynamic_boost
B: PROP=0
B: EV=3
B: KEY=400 6000000000000 0

I: Bus=0019 Vendor=0001 Product=0001 Version=0100
N: Name="keys"
P: Phys=gpio-keys/input0
S: Sysfs=/devices/keys/input/input2
U: Uniq=
H: Handlers=gpufreq_ib event2 dynamic_boost
B: PROP=0
B: EV=100003
B: KEY=c000000000000 0
`

// The hypothesised jack-inserted enumeration from issue #80: accdet claims a
// second input node, so both keypads shift up by one. Not captured from
// hardware — nobody has yet booted a device with a cable in and dumped this
// — so it stands as the shape the parser must survive, not as evidence the
// shift happens.
const shiftedByAccdet = `I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="ACCDET"
H: Handlers=event0

I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="ACCDET_KEY"
H: Handlers=event1

I: Bus=0019 Vendor=2454 Product=6500 Version=0010
N: Name="mtk-kpd"
H: Handlers=gpufreq_ib event2 dynamic_boost

I: Bus=0019 Vendor=0001 Product=0001 Version=0100
N: Name="keys"
H: Handlers=gpufreq_ib event3 dynamic_boost
`

func TestParsesTheRealDeviceOutput(t *testing.T) {
	got := parseInputDevices(officeNoJack)

	for name, want := range map[string]string{
		"ACCDET":  "/dev/input/event0",
		"mtk-kpd": "/dev/input/event1",
		"keys":    "/dev/input/event2",
	} {
		if got[name] != want {
			t.Errorf("%q = %q, want %q", name, got[name], want)
		}
	}
	if len(got) != 3 {
		t.Errorf("parsed %d devices, want 3: %v", len(got), got)
	}
}

// The point of the change: the names still resolve when the numbers move.
func TestNamesSurviveAShiftedEnumeration(t *testing.T) {
	got := parseInputDevices(shiftedByAccdet)

	if got["mtk-kpd"] != "/dev/input/event2" {
		t.Errorf("mtk-kpd = %q, want /dev/input/event2", got["mtk-kpd"])
	}
	if got["keys"] != "/dev/input/event3" {
		t.Errorf("keys = %q, want /dev/input/event3", got["keys"])
	}
	// Under the old hardcoded constants this is what the reader would have
	// opened: ACCDET as the action button, mtk-kpd as volume. Both open
	// successfully and neither produces the expected events, which is the
	// silent failure.
	if got["ACCDET_KEY"] != "/dev/input/event1" {
		t.Errorf("ACCDET_KEY = %q, want /dev/input/event1", got["ACCDET_KEY"])
	}
}

// The event token is found, not indexed — Handlers carries unrelated entries
// on both sides of it on this hardware.
func TestEventTokenIsNotPositional(t *testing.T) {
	got := parseInputDevices(`N: Name="x"
H: Handlers=gpufreq_ib dynamic_boost event7
`)
	if got["x"] != "/dev/input/event7" {
		t.Errorf("got %q, want /dev/input/event7", got["x"])
	}
}

// A stanza with no Handlers line must not let the NEXT stanza's handler be
// credited to it.
func TestNameWithoutHandlersDoesNotLeak(t *testing.T) {
	got := parseInputDevices(`N: Name="nohandler"

N: Name="real"
H: Handlers=event4
`)
	if _, ok := got["nohandler"]; ok {
		t.Errorf("nohandler should be absent, got %q", got["nohandler"])
	}
	if got["real"] != "/dev/input/event4" {
		t.Errorf("real = %q, want /dev/input/event4", got["real"])
	}
}

func TestEmptyInputIsEmptyNotACrash(t *testing.T) {
	if got := parseInputDevices(""); len(got) != 0 {
		t.Errorf("want no devices, got %v", got)
	}
}

// Resolution runs on the host during `go test`, where /proc/bus/input/devices
// does not exist. That must degrade to the historical path rather than fail,
// which is the same behaviour a device with an unreadable procfs gets.
func TestUnresolvableFallsBackToTheOldPath(t *testing.T) {
	if got := resolveInputDevice("nothing-has-this-name", dotButton); got != dotButton {
		t.Errorf("got %q, want the fallback %q", got, dotButton)
	}
}
