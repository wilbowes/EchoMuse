package board

import (
	"os"
	"path/filepath"
	"reflect"
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
	if want := []string{"/dev/input/event1", "/dev/input/event2"}; !reflect.DeepEqual(l.Keys, want) {
		t.Errorf("keys %q, want %q", l.Keys, want)
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
	wantALS := []LightSensor{
		{"tsl2540", "als_lux", BusI2C},
		{"tsl2584tsv", "illuminance0_input", BusIIO},
	}
	if !reflect.DeepEqual(l.LightSensors, wantALS) || l.HCI != "/dev/stpbt" {
		t.Errorf("light sensors %+v, hci %q", l.LightSensors, l.HCI)
	}
	// Biscuit's driver blocks in read(), and os.File has read it since the
	// proxy was written; Bluedroid is what holds the node there, and it is
	// disabled, not stopped.
	if l.HCIPollRead || len(l.HCIDaemons) != 0 {
		t.Errorf("hci poll read %v, daemons %q: biscuit reads its node as a file", l.HCIPollRead, l.HCIDaemons)
	}
	if len(l.Problems) != 0 {
		t.Errorf("parts not found by name on the fixture: %v", l.Problems)
	}
}

// testdata/radar-fireos6 is 035T (FireOS 6, NS6575) on 2026-10-23, read the
// same way. Its PCMs and i2c clients are biscuit's, so are its numbers, but
// radar has no fallbacks: everything must be found by name.
const radarFOS6 = "testdata/radar-fireos6"

func TestRadarResolvesByNameOnly(t *testing.T) {
	b := Detect(radarFOS6)
	if b != Radar {
		t.Fatalf("fixture detected as %v", IDOf(b))
	}
	l := Resolve(radarFOS6, b)
	if want := []string{"/dev/input/event1", "/dev/input/event2"}; !reflect.DeepEqual(l.Keys, want) {
		t.Errorf("keys %q, want %q", l.Keys, want)
	}
	if want := filepath.Join(radarFOS6, "/sys/bus/i2c/devices/0-003f"); l.LEDRing != want {
		t.Errorf("led ring %q, want %q", l.LEDRing, want)
	}
	if l.Capture == nil || *l.Capture != (PCMAddr{0, 24}) || l.Playback == nil || *l.Playback != (PCMAddr{0, 23}) {
		t.Errorf("capture %+v playback %+v", l.Capture, l.Playback)
	}
	if len(l.Problems) != 0 {
		t.Errorf("parts not found by name on the fixture: %v", l.Problems)
	}
	// gpio444 is an audio clock on the Dot 3; no board states a pin it has
	// not measured.
	if l.MuteLEDGPIO != "" {
		t.Errorf("mute LED gpio %q on a board that never measured one", l.MuteLEDGPIO)
	}
	if l.Speaker.AmpMute == "" || l.Speaker.Filter == nil {
		t.Errorf("speaker %+v: radar is silent without its amp mute and filter", l.Speaker)
	}
}

// testdata/donut-fireos6 is 41C8 (FireOS 6.5.7.5, NS6575) on 2026-10-24.
// Its own layout: no part shares biscuit's input or PCM names.
const donutFOS6 = "testdata/donut-fireos6"

func TestDonutResolvesByNameOnly(t *testing.T) {
	b := Detect(donutFOS6)
	if b != Donut {
		t.Fatalf("fixture detected as %v", IDOf(b))
	}
	l := Resolve(donutFOS6, b)
	if want := []string{"/dev/input/event3", "/dev/input/event1"}; !reflect.DeepEqual(l.Keys, want) {
		t.Errorf("keys %q, want %q", l.Keys, want)
	}
	if want := filepath.Join(donutFOS6, "/sys/bus/i2c/devices/0-003f"); l.LEDRing != want {
		t.Errorf("led ring %q, want %q", l.LEDRing, want)
	}
	if l.Playback == nil || *l.Playback != (PCMAddr{0, 6}) {
		t.Errorf("playback %+v, want card 0 device 6", l.Playback)
	}
	// The microphones, and the loopback that must open before them: pcm7,
	// not pcm3's AWB_Record, whose name is a suffix of it.
	if l.Capture == nil || *l.Capture != (PCMAddr{0, 1}) || len(l.Problems) != 0 {
		t.Errorf("capture %+v, problems %q", l.Capture, l.Problems)
	}
	if l.Loopback == nil || *l.Loopback != (PCMAddr{0, 7}) || l.LoopbackRate != 48000 || l.LoopbackChs != 2 {
		t.Errorf("loopback %+v at %d Hz x%d", l.Loopback, l.LoopbackRate, l.LoopbackChs)
	}
	if l.Mics.NumMics() != 4 || l.Mics.Format != S32LE || len(l.MicMutes) != 4 {
		t.Errorf("mics %+v, mutes %q", l.Mics, l.MicMutes)
	}
	if l.MuteLEDGPIO != "" {
		t.Errorf("mute LED gpio %q on a board whose mute LED is the privacy driver's", l.MuteLEDGPIO)
	}
	// Its driver returns 0 bytes when nothing is queued, so the node is
	// read through poll, and Amazon's daemons hold it on FireOS.
	if l.HCI != "/dev/stpbt" || !l.HCIPollRead {
		t.Errorf("hci %q, poll read %v: the Dot 3's node must be read through poll", l.HCI, l.HCIPollRead)
	}
	if want := []string{"btmanagerd", "btclient_ble", "btclient"}; !reflect.DeepEqual(l.HCIDaemons, want) {
		t.Errorf("hci daemons %q, want %q", l.HCIDaemons, want)
	}
	if l.Speaker.AmpSwitch != "" || l.Speaker.Volume.Unity != "255" {
		t.Errorf("speaker %+v: the TAS2770 has no amp switch and unity at 255", l.Speaker)
	}
}

// Opening the Dot 3's microphones before its loopback costs it the amplifier
// until reboot, so a loopback the board states and the kernel does not list
// leaves it with no microphones, and says so.
func TestNoLoopbackMeansNoMicrophones(t *testing.T) {
	root := t.TempDir()
	if err := os.CopyFS(root, os.DirFS(donutFOS6)); err != nil {
		t.Fatal(err)
	}
	pcmList := filepath.Join(root, "proc/asound/pcm")
	b, err := os.ReadFile(pcmList)
	if err != nil {
		t.Fatal(err)
	}
	var kept []string
	for _, line := range strings.Split(string(b), "\n") {
		if !strings.Contains(line, "DL1_AWB_Record") {
			kept = append(kept, line)
		}
	}
	if err := os.WriteFile(pcmList, []byte(strings.Join(kept, "\n")), 0o644); err != nil {
		t.Fatal(err)
	}
	l := Resolve(root, Donut)
	if l.Capture != nil || l.Loopback != nil {
		t.Fatalf("capture %+v, loopback %+v: neither may open", l.Capture, l.Loopback)
	}
	found := false
	for _, p := range l.Problems {
		found = found || strings.HasPrefix(p, "capture:")
	}
	if !found {
		t.Errorf("the controller is not told why: %q", l.Problems)
	}
}

// A board's codec state is all it has to make sound with: no routes is a
// powered-down codec, no volume a speaker at the kernel's level, and a jack
// table whose two sides touch different controls is one the reconciler can
// only half check.
func TestEveryBoardStatesItsCodec(t *testing.T) {
	for _, b := range Known {
		hw := b.Hardware
		if len(hw.Routes) == 0 {
			t.Errorf("%s: no codec routes", b.ID)
		}
		v := hw.Speaker.Volume
		if v.Control == "" || v.Mute == "" || v.Unity == "" || v.Mute == v.Unity {
			t.Errorf("%s: volume %+v", b.ID, v)
		}
		j := hw.Speaker.Jack
		if len(j.Insert) == 0 || len(j.Insert) != len(j.Remove) {
			t.Fatalf("%s: jack %+v", b.ID, j)
		}
		in := map[string]bool{}
		for _, c := range j.Insert {
			in[c.Name] = true
		}
		for _, c := range j.Remove {
			if !in[c.Name] {
				t.Errorf("%s: jack removal writes %q, which insertion does not", b.ID, c.Name)
			}
		}
		all := append(append(append(append([]Ctl{}, hw.Routes...), hw.InputGains...), j.Insert...), j.Remove...)
		for _, c := range append(all, hw.Speaker.Setup...) {
			if c.Name == "" || len(c.Values) == 0 {
				t.Errorf("%s: empty write %+v", b.ID, c)
			}
		}
		// The amp is the first thing silenced on insert and the last thing
		// restored on removal, whichever control silences it.
		amp := hw.Speaker.AmpSwitch
		if amp == "" {
			amp = v.Control
		}
		if j.Insert[0].Name != amp || j.Remove[len(j.Remove)-1].Name != amp {
			t.Errorf("%s: the jack must silence %q first and restore it last", b.ID, amp)
		}
	}
}

func TestEveryBoardStatesItsBassBand(t *testing.T) {
	for _, b := range Known {
		g := b.Hardware.Speaker.BassGuard
		if g.CrossoverHz < 20 || g.CrossoverHz > 500 || g.ThresholdDb >= 0 {
			t.Errorf("%s: bass band %+v", b.ID, g)
		}
	}
}

// A board that reads its mics states an array the canceller and the
// beamformer can index by channel: microphones first and contiguous, each
// channel once, the loopback after them.
func TestEveryCaptureStatesItsMicArray(t *testing.T) {
	for _, b := range Known {
		hw := b.Hardware
		if hw.Capture.Name == "" {
			continue
		}
		a := hw.Mics
		if len(a.Perimeter) == 0 || a.Channels < a.NumMics() {
			t.Fatalf("%s: mic array %+v", b.ID, a)
		}
		seen := map[int]bool{}
		chans := []int{}
		for _, m := range a.Perimeter {
			chans = append(chans, m.Ch)
			if m.Angle < 0 || m.Angle >= 360 {
				t.Errorf("%s: ch%d at %v degrees", b.ID, m.Ch, m.Angle)
			}
		}
		if a.Centre >= 0 {
			chans = append(chans, a.Centre)
		}
		for _, c := range chans {
			if c < 0 || c >= a.NumMics() || seen[c] {
				t.Errorf("%s: mic channel %d is not one of 0-%d once", b.ID, c, a.NumMics()-1)
			}
			seen[c] = true
		}
		if a.EchoRef >= 0 && (a.EchoRef < a.NumMics() || a.EchoRef >= a.Channels) {
			t.Errorf("%s: loopback ch%d is a mic or outside the frame", b.ID, a.EchoRef)
		}
		// A microphone the firmware reads is one mute must silence in
		// hardware too.
		if len(hw.MicMutes) == 0 {
			t.Errorf("%s: reads its microphones and states no hardware mute", b.ID)
		}
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
	if want := []string{"/dev/input/event5", "/dev/input/event0"}; !reflect.DeepEqual(l.Keys, want) {
		t.Errorf("keys %q, want %q", l.Keys, want)
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
	if !reflect.DeepEqual(l.Keys, []string{"/dev/input/event1", "/dev/input/event2"}) ||
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
	if len(l.Keys) != 2 || l.Keys[0] != "/dev/input/event1" || l.MuteLEDGPIO != "444" {
		t.Errorf("layout %+v", l)
	}
}

// A board that states no fallback is never opened by number, and one that
// states no mute LED GPIO has none driven.
func TestNoFallbackMeansNotAvailable(t *testing.T) {
	other := &Board{ID: "other", Hardware: &Hardware{
		Keys:     []Input{{Name: "gpio-keys"}},
		LEDRing:  I2C{Driver: "is31fl3236"},
		Capture:  PCM{Name: "TDM_Capture"},
		Playback: PCM{Name: "DL1_Playback"},
	}}
	l := Resolve(biscuitFOS5, other)
	if len(l.Keys) != 0 || l.Capture != nil || l.Playback != nil {
		t.Errorf("parts this device does not have were resolved: %+v", l)
	}
	if l.MuteLEDGPIO != "" {
		t.Errorf("mute LED gpio %q on a board that states none", l.MuteLEDGPIO)
	}
	if len(l.LightSensors) != 0 || l.HCI != "" {
		t.Errorf("light sensors %+v and hci %q on a board that states neither", l.LightSensors, l.HCI)
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

// The routes biscuit and radar need, both ends: the capture DIFFERENTIAL
// inputs into all four ADCs (the single-ended "IN2" switches beside them are
// the wrong ones), and the DAC into the output mixer. A wrong name is silence
// rather than an error, and either end alone is a device whose every log
// looks healthy.
func TestTLVRoutesCoverBothEndsOfTheAudioPath(t *testing.T) {
	want := map[string]bool{
		"HPR Output Mixer R_DAC Switch": true,
		"HPL Output Mixer L_DAC Switch": true,
	}
	for _, adc := range []string{"A", "B", "C", "D"} {
		for _, side := range []string{"L", "R"} {
			want["ADC_"+adc+" "+map[string]string{"L": "Left", "R": "Right"}[side]+" Ip Select ADC_"+adc+" DIF1_"+side+" switch"] = true
		}
	}
	checkTable(t, tlvRoutes, want, "1")
}

// A level, not a route, so a wrong one is quieter rather than silent: every
// channel of every ADC, and OFF, as the HAL leaves it (#806).
func TestTLVInputGainsCoverEveryChannelAndAreOff(t *testing.T) {
	want := map[string]bool{}
	for _, adc := range []string{"A", "B", "C", "D"} {
		for _, side := range []string{"L", "R"} {
			want["ADC_"+adc+" DIF1_"+side+" Input Gain"] = true
		}
	}
	checkTable(t, tlvInputGains, want, "0")
}

func checkTable(t *testing.T, table []Ctl, want map[string]bool, value string) {
	t.Helper()
	got := map[string]bool{}
	for _, c := range table {
		if got[c.Name] {
			t.Errorf("%s listed twice", c.Name)
		}
		if len(c.Values) != 1 || c.Values[0] != value {
			t.Errorf("%s: %v, want %q", c.Name, c.Values, value)
		}
		got[c.Name] = true
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("table %v\nwant  %v", got, want)
	}
}

// Biscuit's jack, measured against stock (codec.go). A gain left unset, or
// set to 0 (the floor of 0..35), is a silent jack; the pair takes both
// channels; the DAC mux goes On with a plug in (#566) and Off without.
func TestTLVJackIsStocks(t *testing.T) {
	value := func(ctls []Ctl, name string) []string {
		for _, c := range ctls {
			if c.Name == name {
				return c.Values
			}
		}
		return nil
	}
	cases := []struct {
		ctls      []Ctl
		gain, mux string
	}{{tlvJack.Insert, "11", "On"}, {tlvJack.Remove, "6", "Off"}}
	for _, c := range cases {
		g := value(c.ctls, "HP Driver Gain Volume")
		if len(g) != 2 || g[0] != c.gain || g[1] != c.gain {
			t.Errorf("HP driver gain %v, want %s on both channels", g, c.gain)
		}
		if m := value(c.ctls, "Audio_DacMux_Setting"); len(m) != 1 || m[0] != c.mux {
			t.Errorf("DAC mux %v, want %s", m, c.mux)
		}
	}
	if tlvJack.Insert[0].Values[0] != "Off" || tlvJack.Remove[2].Values[0] != "On" {
		t.Errorf("amp: insert %v, remove %v", tlvJack.Insert[0], tlvJack.Remove[2])
	}
}
