package cue

import (
	"encoding/binary"
	"math"
	"os"
	"testing"
)

const rate = 48000

// The cue is the first sound this device makes itself, so it is also the first
// place a click can originate on the device rather than arrive from the wire.
// A sine burst that starts or stops at full amplitude IS a step, and a step is
// a click — the same discontinuity the output chain's crossfade exists to
// remove, reached from the other direction.
func TestNoDiscontinuities(t *testing.T) {
	c := WakeCue(rate, LevelDBFS(LevelMedium))
	if len(c) == 0 {
		t.Fatal("empty cue")
	}
	if c[0] != 0 {
		t.Errorf("first sample is %v, want 0 — the cue starts with a step", c[0])
	}
	if last := c[len(c)-1]; math.Abs(last) > 1e-9 {
		t.Errorf("last sample is %v, want 0 — the cue ends with a step", last)
	}

	// The steepest a 880Hz tone at this amplitude can legitimately move in one
	// sample. Anything much past it is not signal.
	peak := 0.0
	for _, v := range c {
		peak = math.Max(peak, math.Abs(v))
	}
	bound := peak * 2 * math.Pi * BongHz * 2 / rate // 2x headroom for the harmonic
	worst, at := 0.0, 0
	for i := 1; i < len(c); i++ {
		if d := math.Abs(c[i] - c[i-1]); d > worst {
			worst, at = d, i
		}
	}
	t.Logf("peak %.0f, worst step %.1f at sample %d (bound %.1f)", peak, worst, at, bound)
	if worst > bound {
		t.Errorf("discontinuity of %.1f at sample %d exceeds the %.1f a %.0fHz "+
			"tone can produce", worst, at, bound, BongHz)
	}
}

// It must RISE. A falling interval reads as dismissal, which is the opposite
// of what the cue is for — so the direction is asserted, not left to whoever
// next edits the constants.
func TestTheCueRises(t *testing.T) {
	if BongHz <= BingHz {
		t.Fatalf("bong %.0fHz is not above bing %.0fHz — a falling cue reads "+
			"as 'no', not as 'listening'", BongHz, BingHz)
	}
	// Measure it rather than trusting the constants: the second half must
	// have more energy above the midpoint frequency than the first.
	c := WakeCue(rate, LevelDBFS(LevelMedium))
	mid := (BingHz + BongHz) / 2
	first := dominantHz(c[:len(c)/3], rate)
	last := dominantHz(c[len(c)*2/3:], rate)
	t.Logf("first third ~%.0fHz, last third ~%.0fHz (midpoint %.0f)", first, last, mid)
	if !(first < mid && last > mid) {
		t.Errorf("rendered cue does not rise: %.0fHz then %.0fHz", first, last)
	}
}

// dominantHz is a crude peak-picker — enough to tell 660 from 880 without
// pulling in an FFT.
func dominantHz(x []float64, fs float64) float64 {
	best, bestHz := 0.0, 0.0
	for hz := 400.0; hz <= 1200.0; hz += 5 {
		var re, im float64
		for i, v := range x {
			p := 2 * math.Pi * hz * float64(i) / fs
			re += v * math.Cos(p)
			im += v * math.Sin(p)
		}
		if m := re*re + im*im; m > best {
			best, bestHz = m, hz
		}
	}
	return bestHz
}

// Each level peaks where it says, loud never clips, and the levels are
// distinct and in order — a preset that plays the same as its neighbour is a
// setting that does nothing.
func TestPeakLevels(t *testing.T) {
	prev := math.Inf(-1)
	for _, lv := range Levels {
		c := WakeCue(rate, LevelDBFS(lv))
		peak := 0.0
		for _, v := range c {
			peak = math.Max(peak, math.Abs(v))
		}
		want := math.Pow(10, LevelDBFS(lv)/20.0) * 32768.0
		if peak > want*1.02 || peak < want*0.8 {
			t.Errorf("%s: peak %.0f, want about %.0f (%.1f dBFS)", lv, peak, want, LevelDBFS(lv))
		}
		if peak > 32767 {
			t.Fatalf("%s clips", lv)
		}
		if peak <= prev*1.5 {
			t.Errorf("%s is not clearly louder than the level below it", lv)
		}
		prev = peak
	}
	if LevelDBFS("nonsense") != LevelDBFS(LevelMedium) || LevelDBFS("") != LevelDBFS(LevelMedium) {
		t.Error("an unknown level must play as medium")
	}
}

// DurationMS is what the mic path uses to know which frames to exclude from
// the wake scorer and the ASR stream, so it must match what is rendered.
func TestDurationMatchesTheRender(t *testing.T) {
	c := WakeCue(rate, LevelDBFS(LevelMedium))
	got := float64(len(c)) / rate * 1000
	if math.Abs(got-DurationMS()) > 1.0 {
		t.Errorf("rendered %.1fms, DurationMS() says %.1fms — the mic path "+
			"would exclude the wrong frames", got, DurationMS())
	}
}

// Rendered at whatever rate it is asked for, since the constant lives in the
// speaker package and this one should not assume it.
func TestRendersAtOtherRates(t *testing.T) {
	for _, fs := range []int{16000, 44100, 48000} {
		c := WakeCue(fs, LevelDBFS(LevelMedium))
		got := float64(len(c)) / float64(fs) * 1000
		if math.Abs(got-DurationMS()) > 1.5 {
			t.Errorf("%dHz: rendered %.1fms, want %.1fms", fs, got, DurationMS())
		}
	}
}

func TestVolumeCueTracksTheSelectedVolume(t *testing.T) {
	full := VolumeCue(rate, 1)
	quiet := VolumeCue(rate, 0.1)
	if len(full) == 0 || len(quiet) != len(full) {
		t.Fatalf("volume cues have lengths full=%d quiet=%d", len(full), len(quiet))
	}
	peak := func(samples []float64) float64 {
		var out float64
		for _, v := range samples {
			out = math.Max(out, math.Abs(v))
		}
		return out
	}
	ratio := peak(quiet) / peak(full)
	if math.Abs(ratio-0.1) > 0.001 {
		t.Errorf("quiet/full peak ratio = %.4f, want 0.1", ratio)
	}
	if full[0] != 0 || math.Abs(full[len(full)-1]) > 1e-9 {
		t.Error("volume cue must fade from and to zero")
	}
	if got := VolumeCue(rate, 0); got != nil {
		t.Errorf("zero volume rendered %d samples, want silence", len(got))
	}
	wantPeak := math.Pow(10, volumePeakDB/20.0) * 32768.0
	if got := peak(full); math.Abs(got-wantPeak) > 0.01 {
		t.Errorf("full-volume peak = %.2f, want %.2f (%.1fdBFS)", got, wantPeak, volumePeakDB)
	}
}

func TestVolumeButtonPreviewRepeatsAtMaximum(t *testing.T) {
	for _, tc := range []struct {
		name      string
		direction string
		changed   bool
		atMax     bool
		want      bool
	}{
		{"ordinary up", "up", true, false, true},
		{"ordinary down", "down", true, false, true},
		{"up again at maximum", "up", false, true, true},
		{"unchanged up below maximum", "up", false, false, false},
		{"down at lower boundary", "down", false, false, false},
		{"down while at maximum", "down", false, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := VolumeButtonPreviewDue(tc.direction, tc.changed, tc.atMax); got != tc.want {
				t.Errorf("VolumeButtonPreviewDue(%q, %v, %v) = %v, want %v",
					tc.direction, tc.changed, tc.atMax, got, tc.want)
			}
		})
	}
}

func TestVolumeCueIsASingleDecayingBeep(t *testing.T) {
	c := VolumeCue(rate, 1)
	wantLen := int(rate * volumeMS / 1000)
	if len(c) != wantLen {
		t.Fatalf("volume cue has %d samples, want %d", len(c), wantLen)
	}
	if volumeHz <= BingHz/2 || volumeHz >= BongHz/2 {
		t.Fatalf("volume cue %.0fHz is not between the half-pitches of the wake cue", volumeHz)
	}

	// It begins as a definite beep, then the same note remains audible while
	// decaying. A flat electronic alert would have comparable RMS throughout;
	// a hard cutoff would have no measurable release window.
	rms := func(x []float64) float64 {
		var sum float64
		for _, v := range x {
			sum += v * v
		}
		return math.Sqrt(sum / float64(len(x)))
	}
	window := func(startMS, endMS float64) []float64 {
		return c[int(rate*startMS/1000):int(rate*endMS/1000)]
	}
	body := rms(window(10, 35))
	release := rms(window(75, 120))
	if release < body*0.08 || release > body*0.4 {
		t.Errorf("volume cue release RMS %.1f is not a short, audible decay from body %.1f", release, body)
	}
	for i, v := range c {
		if v > math.MaxInt16 || v < math.MinInt16 {
			t.Fatalf("full-volume cue clips at sample %d: %.1f", i, v)
		}
	}
}

// Writes the cue as a WAV for a listening test. Off by default — this is for
// auditioning a taste parameter, which no assertion can settle.
//
//	EM_CUE_WAV=/tmp/wake_cue.wav go test ./internal/cue/ -run Audition
func TestAudition(t *testing.T) {
	path := os.Getenv("EM_CUE_WAV")
	if path == "" {
		t.Skip("set EM_CUE_WAV to render a WAV for listening")
	}
	c := WakeCue(rate, LevelDBFS(LevelMedium))
	if err := writeWAV(path, c, rate); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
	t.Logf("wrote %s (%.0fms, %d samples)", path, DurationMS(), len(c))
}

func writeWAV(path string, samples []float64, fs int) error {
	data := make([]byte, len(samples)*2)
	for i, v := range samples {
		s := int16(math.Max(-32768, math.Min(32767, v)))
		binary.LittleEndian.PutUint16(data[i*2:], uint16(s))
	}
	var h []byte
	u32 := func(v uint32) { b := make([]byte, 4); binary.LittleEndian.PutUint32(b, v); h = append(h, b...) }
	u16 := func(v uint16) { b := make([]byte, 2); binary.LittleEndian.PutUint16(b, v); h = append(h, b...) }
	h = append(h, "RIFF"...)
	u32(uint32(36 + len(data)))
	h = append(h, "WAVEfmt "...)
	u32(16)
	u16(1)
	u16(1)
	u32(uint32(fs))
	u32(uint32(fs * 2))
	u16(2)
	u16(16)
	h = append(h, "data"...)
	u32(uint32(len(data)))
	return os.WriteFile(path, append(h, data...), 0o644)
}
