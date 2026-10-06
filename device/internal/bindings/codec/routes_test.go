package codec

import (
	"errors"
	"sync"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/bindings/mixer"
)

// A wrong name here is silence rather than an error, and the two ends failed
// independently: the capture routes leave the ADCs powered down, the playback
// routes leave the DAC powered down, and either alone is a device that looks
// healthy in every log it writes.
func TestRoutesCoverBothEndsOfTheAudioPath(t *testing.T) {
	want := map[string]bool{
		// capture: the DIFFERENTIAL inputs into all four ADCs — the
		// single-ended "IN2" switches beside them are the wrong ones
		"ADC_D Right Ip Select ADC_D DIF1_R switch": true,
		"ADC_D Left Ip Select ADC_D DIF1_L switch":  true,
		"ADC_C Right Ip Select ADC_C DIF1_R switch": true,
		"ADC_C Left Ip Select ADC_C DIF1_L switch":  true,
		"ADC_B Right Ip Select ADC_B DIF1_R switch": true,
		"ADC_B Left Ip Select ADC_B DIF1_L switch":  true,
		"ADC_A Right Ip Select ADC_A DIF1_R switch": true,
		"ADC_A Left Ip Select ADC_A DIF1_L switch":  true,
		// playback: DAC into the output mixer
		"HPR Output Mixer R_DAC Switch": true,
		"HPL Output Mixer L_DAC Switch": true,
	}

	got := map[string]bool{}
	for _, w := range Routes {
		if got[w.Name] {
			t.Errorf("%s listed twice", w.Name)
		}
		if w.Value != "1" {
			t.Errorf("%s: value %q, want \"1\" — every route here is a switch to close", w.Name, w.Value)
		}
		got[w.Name] = true
	}
	for name := range want {
		if !got[name] {
			t.Errorf("missing %s", name)
		}
	}
	for name := range got {
		if !want[name] {
			t.Errorf("unexpected %s", name)
		}
	}
}

// ─── EnsureRoutes ────────────────────────────────────────────────────────────

// The table test above reads as coverage and is not: `Routes` is a slice of
// constant strings, which is a static symbol with no statements in it, and
// `var once sync.Once` is zero-valued with none either. Every coverable
// statement in this package is inside EnsureRoutes, so the package measured
// 0.0% while looking well tested.
//
// The device build installs tinyalsa in an init; on a host the default backend
// is `unavailable{}`, which makes EnsureRoutes run deterministically and fail
// every write — so a fake is installed here to observe what it does.

type recordingBackend struct {
	sets [][]string
	fail map[string]bool
}

func (b *recordingBackend) Set(name string, values []string) error {
	if b.fail[name] {
		return errors.New("no such control")
	}
	b.sets = append(b.sets, append([]string{name}, values...))
	return nil
}

func (b *recordingBackend) Get(name string) (string, error) {
	return "", errors.New("no such control")
}

// resetOnce clears the sync.Once so each test can drive EnsureRoutes afresh.
// Unexported and in-package, which is the only reason this file is `package
// codec` rather than `codec_test`.
func resetOnce() { once = sync.Once{} }

// The fake backend is installed without being restored. Nothing else in this
// package reads the mixer — the other test walks the Routes table — and each
// package's tests are a separate binary, so there is no state to leak into.
// Exposing a reset on the mixer for this would be production API that exists
// only for a test.

func TestEnsureRoutesClosesEveryRoute(t *testing.T) {
	resetOnce()
	b := &recordingBackend{}
	mixer.Use(b)

	EnsureRoutes()

	all := append(append([]Write{}, Routes...), InputGains...)
	if len(b.sets) != len(all) {
		t.Fatalf("%d writes for %d routes and input gains", len(b.sets), len(all))
	}
	for i, w := range all {
		if len(b.sets[i]) != 2 || b.sets[i][0] != w.Name || b.sets[i][1] != w.Value {
			t.Errorf("write %d = %v, want [%s %s]", i, b.sets[i], w.Name, w.Value)
		}
	}
}

// `sync.Once` is load-bearing: DAPM decides what to power at stream open, so the
// routes must be closed before EITHER the mic or the speaker opens its PCM — and
// both of them call this, so whichever runs second must not redo the work.
func TestEnsureRoutesRunsOnlyOncePerProcess(t *testing.T) {
	resetOnce()
	b := &recordingBackend{}
	mixer.Use(b)

	EnsureRoutes()
	first := len(b.sets)
	EnsureRoutes()
	EnsureRoutes()

	if len(b.sets) != first {
		t.Fatalf("repeated calls wrote %d routes, want %d — the second caller "+
			"is the speaker opening its PCM and must not re-run this",
			len(b.sets), first)
	}
}

// A control that does not resolve must be counted, not swallowed. #546 is why:
// addressing a route by its positional id wrote to a neighbouring control,
// which is a perfectly valid write — tinymix exits 0 and the failure count
// stays 0, so a device logs a clean boot and plays nothing. Going by NAME means
// an absent control is now an error, and this is the count that says so.
//
// The control assertion is the second half: the same call with a backend that
// accepts everything writes all of them, so a count of zero here cannot be an
// artefact of the fake.
func TestEnsureRoutesCountsAndReportsFailures(t *testing.T) {
	resetOnce()
	b := &recordingBackend{fail: map[string]bool{}}
	// Fail exactly two of them, chosen from the table rather than by index so
	// the test does not depend on the ordering of Routes.
	b.fail[Routes[0].Name] = true
	b.fail[Routes[len(Routes)-1].Name] = true
	mixer.Use(b)

	EnsureRoutes()

	if want := len(Routes) + len(InputGains) - 2; len(b.sets) != want {
		t.Fatalf("%d writes, want %d — a failing control must not stop the "+
			"remaining routes being attempted", len(b.sets), want)
	}

	// The control: nothing fails, everything is written.
	resetOnce()
	ok := &recordingBackend{}
	mixer.Use(ok)
	EnsureRoutes()
	if want := len(Routes) + len(InputGains); len(ok.sets) != want {
		t.Fatalf("control: %d writes with nothing failing, want %d",
			len(ok.sets), want)
	}
}

// The input gains are a level, not a route, so a wrong one is quieter rather
// than silent — which is how eight of them went unwritten until #806.
// Every channel of every ADC, both sides, and OFF: On is the kernel's default
// and reads about 6dB lower than the HAL's Off.
func TestInputGainsCoverEveryChannelAndAreOff(t *testing.T) {
	want := map[string]bool{}
	for _, adc := range []string{"A", "B", "C", "D"} {
		for _, side := range []string{"L", "R"} {
			want["ADC_"+adc+" DIF1_"+side+" Input Gain"] = true
		}
	}
	got := map[string]bool{}
	for _, w := range InputGains {
		if got[w.Name] {
			t.Errorf("%s listed twice", w.Name)
		}
		if w.Value != "0" {
			t.Errorf("%s: value %q, want \"0\" (Off, as the HAL leaves it)", w.Name, w.Value)
		}
		got[w.Name] = true
	}
	for name := range want {
		if !got[name] {
			t.Errorf("missing %s", name)
		}
	}
	if len(got) != len(want) {
		t.Errorf("%d input gains, want %d", len(got), len(want))
	}
}

// A failed input gain is reported on its own and does not stop the others.
func TestEnsureRoutesStillWritesTheOtherInputGainsWhenOneFails(t *testing.T) {
	resetOnce()
	b := &recordingBackend{fail: map[string]bool{InputGains[3].Name: true}}
	mixer.Use(b)

	EnsureRoutes()

	if want := len(Routes) + len(InputGains) - 1; len(b.sets) != want {
		t.Fatalf("%d writes, want %d", len(b.sets), want)
	}
}
