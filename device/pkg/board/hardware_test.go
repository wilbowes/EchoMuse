package board

import (
	"path/filepath"
	"strings"
	"testing"
)

// testdata/biscuit-fireos5 is what VVV (FireOS 5.5.5.4) reported on
// 2026-10-04: idme, /proc/asound/pcm, /proc/bus/input/devices and every i2c
// client's name.
const biscuitFOS5 = "testdata/biscuit-fireos5"

// Resolved by name, biscuit's parts must be exactly where every build before
// this one opened them by number.
func TestBiscuitResolvesToTheNumbersItAlwaysUsed(t *testing.T) {
	b := Detect(biscuitFOS5)
	if b != Biscuit {
		t.Fatalf("fixture detected as %v", IDOf(b))
	}
	l := Resolve(biscuitFOS5, b)
	if l.DotKeys != "/dev/input/event1" || l.VolumeKeys != "/dev/input/event2" {
		t.Errorf("keys: dot %q volume %q", l.DotKeys, l.VolumeKeys)
	}
	if want := filepath.Join(biscuitFOS5, "/sys/bus/i2c/devices/0-003f"); l.LEDRing != want {
		t.Errorf("led ring %q, want %q", l.LEDRing, want)
	}
	if l.Capture == nil || *l.Capture != (PCMAddr{0, 24}) {
		t.Errorf("capture %+v, want card 0 device 24", l.Capture)
	}
	if l.Playback == nil || *l.Playback != (PCMAddr{0, 23}) {
		t.Errorf("playback %+v, want card 0 device 23", l.Playback)
	}
	if l.MuteLEDGPIO != "444" {
		t.Errorf("mute LED gpio %q", l.MuteLEDGPIO)
	}
	if l.LightSensor != (LightSensor{"tsl2540", "als_lux"}) || l.HCI != "/dev/stpbt" {
		t.Errorf("light sensor %+v, hci %q", l.LightSensor, l.HCI)
	}
	if len(l.Problems) != 0 {
		t.Errorf("parts not found by name on the fixture: %v", l.Problems)
	}
}

// The names decide, not the numbers: the same parts enumerated in another
// order resolve to wherever they now are.
func TestPartsAreFoundWhereverTheyEnumerate(t *testing.T) {
	root := mkroot(t, map[string]string{
		"/proc/bus/input/devices": "" +
			"I: Bus=0019\nN: Name=\"keys\"\nH: Handlers=event0 \n\n" +
			"I: Bus=0019\nN: Name=\"touch\"\nH: Handlers=event2 \n\n" +
			"I: Bus=0019\nN: Name=\"mtk-kpd\"\nH: Handlers=kbd event5 \n\n",
		"/proc/asound/pcm": "" +
			"00-00: TLV320AIC3101 Capture tlv320aic3101-codec-0 :  : capture 1\n" +
			"01-07: TLV320AIC3204 Playback tlv320aic32x4-hifi-7 :  : playback 1\n",
		"/sys/bus/i2c/devices/3-003f/name": "is31fl3236\n",
	})
	l := Resolve(root, Biscuit)
	if l.DotKeys != "/dev/input/event5" || l.VolumeKeys != "/dev/input/event0" {
		t.Errorf("keys: dot %q volume %q", l.DotKeys, l.VolumeKeys)
	}
	if l.Capture == nil || *l.Capture != (PCMAddr{0, 0}) {
		t.Errorf("capture %+v", l.Capture)
	}
	if l.Playback == nil || *l.Playback != (PCMAddr{1, 7}) {
		t.Errorf("playback %+v", l.Playback)
	}
	if !strings.HasSuffix(l.LEDRing, "/sys/bus/i2c/devices/3-003f") {
		t.Errorf("led ring %q", l.LEDRing)
	}
}

// A kernel that names nothing as expected leaves biscuit on the numbers it
// has always used, and every part says so.
func TestBiscuitFallsBackToItsNumbersAndSaysSo(t *testing.T) {
	l := Resolve(t.TempDir(), Biscuit)
	if l.DotKeys != "/dev/input/event1" || l.VolumeKeys != "/dev/input/event2" ||
		l.LEDRing != "/sys/devices/soc/11007000.i2c/i2c-0/0-003f" ||
		l.Capture == nil || *l.Capture != (PCMAddr{0, 24}) ||
		l.Playback == nil || *l.Playback != (PCMAddr{0, 23}) {
		t.Errorf("fallback layout wrong: %+v", l)
	}
	if len(l.Notes) != 5 || len(l.Problems) != 5 {
		t.Fatalf("want a note and a problem per part, got %d and %d", len(l.Notes), len(l.Problems))
	}
	for _, n := range l.Notes {
		if !strings.Contains(n, "as on every unit measured") {
			t.Errorf("fallback not reported: %s", n)
		}
	}
}

// An unidentified device gets biscuit's layout, as before boards were told
// apart, and Board stays nil so nothing mistakes it for a detection.
func TestUnknownBoardKeepsBiscuitsLayout(t *testing.T) {
	l := Resolve(biscuitFOS5, nil)
	if l.Board != nil {
		t.Errorf("Board = %v, want nil", l.Board.ID)
	}
	if l.DotKeys != "/dev/input/event1" || l.MuteLEDGPIO != "444" {
		t.Errorf("layout %+v", l)
	}
}

// A board that states no fallback is never opened by number, and one that
// states no mute LED GPIO has none driven.
func TestNoFallbackMeansNotAvailable(t *testing.T) {
	other := &Board{ID: "other", Hardware: &Hardware{
		DotKeys:  Input{Name: "gpio-keys"},
		LEDRing:  I2C{Driver: "is31fl3236"},
		Capture:  PCM{Name: "TDM_Capture"},
		Playback: PCM{Name: "DL1_Playback"},
	}}
	l := Resolve(biscuitFOS5, other)
	if l.DotKeys != "" || l.VolumeKeys != "" || l.Capture != nil || l.Playback != nil {
		t.Errorf("parts this device does not have were resolved: %+v", l)
	}
	if l.MuteLEDGPIO != "" {
		t.Errorf("mute LED gpio %q on a board that states none", l.MuteLEDGPIO)
	}
	if l.LightSensor != (LightSensor{}) || l.HCI != "" {
		t.Errorf("light sensor %+v and hci %q on a board that states neither", l.LightSensor, l.HCI)
	}
	if !strings.HasSuffix(l.LEDRing, "0-003f") {
		t.Errorf("the ring is the same chip and should resolve: %q", l.LEDRing)
	}
}

func TestInputEvent(t *testing.T) {
	root := mkroot(t, map[string]string{"/proc/bus/input/devices": "" +
		"N: Name=\"keys\"\nH: Handlers=gpufreq_ib event2 dynamic_boost \n\n" +
		"N: Name=\"keys2\"\nH: Handlers=event3 \n\n" +
		"N: Name=\"no handler\"\nH: Handlers=kbd \n\n" +
		"N: Name=\"twin\"\nH: Handlers=event4 \n\n" +
		"N: Name=\"twin\"\nH: Handlers=event5 \n\n" +
		"N: Name=\"say \"hi\"\"\nH: Handlers=event6 \n\n" +
		// No blank line before the next device: its handlers are its own.
		"N: Name=\"first\"\nN: Name=\"second\"\nH: Handlers=event7 \n"})
	cases := []struct {
		name, want string
		ok         bool
	}{
		{"keys", "/dev/input/event2", true}, // not keys2, not a prefix match
		{"keys2", "/dev/input/event3", true},
		{"key", "", false},
		{"no handler", "", false},
		{"twin", "", false}, // ambiguous is refused
		{`say "hi"`, "/dev/input/event6", true},
		{"first", "", false},
		{"second", "/dev/input/event7", true},
		{"", "", false},
	}
	for _, c := range cases {
		got, err := InputEvent(root, c.name)
		if (err == nil) != c.ok || got != c.want {
			t.Errorf("InputEvent(%q) = %q, %v; want %q ok=%v", c.name, got, err, c.want, c.ok)
		}
	}
	if _, err := InputEvent(t.TempDir(), "keys"); err == nil {
		t.Error("a missing /proc/bus/input/devices must be an error")
	}
}

func TestPCMDevice(t *testing.T) {
	root := mkroot(t, map[string]string{"/proc/asound/pcm": "" +
		"00-01: TDM_Capture (*) :  : capture 1\n" +
		"00-02: Voice_MD1 dai-2 :  : playback 1 : capture 1\n" +
		"00-07: DL1_AWB_Record (*) :  : capture 1\n" +
		"00-09: DL1 (*) :  : playback 1\n" +
		"00-23: TLV320AIC3204 Playback tlv320aic32x4-hifi-23 :  : playback 1\n" +
		"01-03: Twin a :  : playback 1\n" +
		"01-04: Twin b :  : playback 1\n" +
		"garbage\n"})
	cases := []struct {
		name    string
		capture bool
		want    *PCMAddr
	}{
		{"TDM_Capture", true, &PCMAddr{0, 1}},
		{"TDM_Capture", false, nil}, // wrong direction
		{"Voice_MD1", true, &PCMAddr{0, 2}},
		{"Voice_MD1", false, &PCMAddr{0, 2}},
		{"DL1", false, &PCMAddr{0, 9}}, // not DL1_AWB_Record
		{"DL1", true, nil},
		{"TLV320AIC3204 Playback", false, &PCMAddr{0, 23}},
		{"TLV320AIC3204", false, &PCMAddr{0, 23}},
		{"TLV320AIC", false, nil}, // whole words only
		{"Twin", false, nil},      // ambiguous is refused
		{"", false, nil},
	}
	for _, c := range cases {
		got, err := PCMDevice(root, c.name, c.capture)
		if (got == nil) != (c.want == nil) || (got != nil && *got != *c.want) || (got == nil) != (err != nil) {
			t.Errorf("PCMDevice(%q, capture=%v) = %+v, %v; want %+v", c.name, c.capture, got, err, c.want)
		}
	}
}

func TestI2CDevice(t *testing.T) {
	root := mkroot(t, map[string]string{
		"/sys/bus/i2c/devices/0-003f/name": "is31fl3236\n",
		"/sys/bus/i2c/devices/0-0018/name": "tlv320aic3101\n",
		"/sys/bus/i2c/devices/0-0019/name": "tlv320aic3101\n",
		"/sys/bus/i2c/devices/2-0018/name": "tlv320aic32x4\n",
	})
	if got, err := I2CDevice(root, "is31fl3236"); err != nil || !strings.HasSuffix(got, "/0-003f") {
		t.Errorf("got %q, %v", got, err)
	}
	// One of four identical ADCs is not identified by its name.
	if _, err := I2CDevice(root, "tlv320aic3101"); err == nil {
		t.Error("an ambiguous name must be refused")
	}
	if _, err := I2CDevice(root, "tlv320aic32"); err == nil {
		t.Error("a prefix must not match")
	}
	if _, err := I2CDevice(root, "absent"); err == nil {
		t.Error("an absent name must be an error")
	}
}
