package speaker

// Sound-quality ratchet (#892): every board's output path, speaker and jack,
// rendered offline at a spread of volume levels and measured against
// testdata/quality_baseline.json. A change that moves a number fails here
// until the baseline is regenerated in the same change, so the diff shows
// what it did to the sound:
//
//     go test ./internal/bindings/speaker -run TestOutputQuality -update-quality
//
// The 10-16 stock-sound trial reached a device with grit at every level (the
// upper bands' instant attack): the syllable stimulus below is the one that
// found it (-40dB at level 25), and the distortion bound would have failed
// that build.
//
// The write loop itself is //go:build server (cgo tinyalsa), so the order is
// restated here from pcm_speaker.go: stockPeriod (volume in float, then
// ProcessWide) and the plain path (Process, then the volume).

import (
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"math/cmplx"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

var updateQuality = flag.Bool("update-quality", false, "rewrite testdata/quality_baseline.json")

const (
	qFs      = 48000
	qSeconds = 1.5   // settle, then measure the last qFFT samples
	qFFT     = 32768 // 0.68s, 1.46Hz bins
	qRefLvl  = 96    // level the response is read at
	qPeriod  = 2048  // pcm_speaker.go periodSize

	// Tolerances against the baseline. The chain is deterministic, so these
	// only absorb floating-point noise across platforms.
	qTolDb = 0.05
	// Bounds that hold whatever the baseline says.
	// The bounds are the backstop for a baseline regenerated carelessly; the
	// baseline itself catches anything finer. Syllable products: no worse
	// than the 10-16 grit (-40dB at level 25). Today's worst is -43dB at full
	// volume, where the stock law drives the compressors over unity on purpose.
	qDistMaxDb = -40
	qTHDMaxDb  = -50 // 1kHz sine
	// The bounds apply only where the programme is above this. Below it the
	// S16 output of an undithered tone is itself harmonic (-38dB at -66dBFS),
	// a hundred dB under full scale and inaudible; the baseline still holds it.
	qBoundFloorDbfs = -45
)

var qLevels = []int{16, 32, 48, 64, 80, 96, 107, 112, 120, 127}

// qTones are the programme's components, log-spaced 50Hz-12kHz.
var qTones = func() []float64 {
	const n = 24
	t := make([]float64, n)
	for i := range t {
		t[i] = 50 * math.Pow(12000.0/50, float64(i)/(n-1))
	}
	return t
}()

type qPath struct {
	Loudness map[string]float64 `json:"programme_rms_dbfs"`
	Dist     map[string]float64 `json:"syllable_distortion_db"`
	THD      map[string]float64 `json:"sine_1k_thd_db"`
	Response []float64          `json:"response_db_at_96"`
}

// qSignal returns n samples at S16 scale.
func qSignal(name string) []float64 {
	n := int(qSeconds * qFs)
	x := make([]float64, n)
	switch name {
	case "programme":
		// Equal tones, Schroeder phases for a low crest factor, -14dBFS RMS.
		k := float64(len(qTones))
		a := 32767 * math.Pow(10, -14.0/20) * math.Sqrt(2/k)
		for i := range x {
			t := float64(i) / qFs
			for j, f := range qTones {
				ph := math.Pi * float64(j) * float64(j) / k
				x[i] += a * math.Sin(2*math.Pi*f*t+ph)
			}
		}
	case "syllable":
		// 200+300Hz under a 4Hz syllable envelope, peaking at -1dBFS: a hot
		// voice in the band the correction lifts most.
		a := 32767 * math.Pow(10, -1.0/20) / 2
		for i := range x {
			t := float64(i) / qFs
			env := 0.5 * (1 - math.Cos(2*math.Pi*4*t))
			x[i] = a * env * (math.Sin(2*math.Pi*200*t) + math.Sin(2*math.Pi*300*t))
		}
	case "sine1k":
		a := 32767 * math.Pow(10, -6.0/20)
		for i := range x {
			x[i] = a * math.Sin(2*math.Pi*1000*float64(i)/qFs)
		}
	}
	return x
}

// qRender plays x through a board's output at a level, as the speaker does,
// and returns the output at S16 scale. It fails the test if the limiter let
// anything clip.
func qRender(t *testing.T, sp board.Speaker, plugged bool, level int, x []float64) []float64 {
	t.Helper()
	params := outchain.DefaultParams()
	c := outchain.NewFor(qFs, guardBand(sp.BassGuard), sp.Voicing)
	c.SetActive(true)
	c.SetVolume(level)
	c.SetParams(chainForJack(params, plugged))
	stock := stockLawOn(sp.Voicing, true, params, plugged)

	var vol softVolume
	if stock {
		vol.set(StockVolumeGain(level, sp.Voicing.CeilingDb))
	} else {
		vol.set(VolumeGain(level))
	}
	vol.settle()

	y := make([]float64, 0, len(x))
	wide := make([]float64, qPeriod*2)
	buf := make([]byte, qPeriod*4)
	for off := 0; off+qPeriod <= len(x); off += qPeriod {
		for i := 0; i < qPeriod; i++ {
			s := x[off+i]
			if stock {
				g := vol.cur
				wide[i*2], wide[i*2+1] = s*g, s*g
			} else {
				v := uint16(int16(math.Round(s)))
				binary.LittleEndian.PutUint16(buf[i*4:], v)
				binary.LittleEndian.PutUint16(buf[i*4+2:], v)
			}
		}
		if stock {
			c.ProcessWide(wide, buf)
		} else {
			c.Process(buf)
			vol.apply(buf)
		}
		for i := 0; i < qPeriod; i++ {
			y = append(y, float64(int16(binary.LittleEndian.Uint16(buf[i*4:]))))
		}
	}
	if st := c.TakeStats(); st.Clipped != 0 {
		t.Errorf("level %d: %d samples clipped while limiting", level, st.Clipped)
	}
	return y
}

// qSpectrum is the power spectrum of the last qFFT samples, windowed with a
// 4-term Blackman-Harris: a Hann window's leakage alone reads -73dB on a
// pure 1kHz sine, which is the floor every THD+N here measured at first.
func qSpectrum(y []float64) []float64 {
	seg := y[len(y)-qFFT:]
	z := make([]complex128, qFFT)
	for i, v := range seg {
		a := 2 * math.Pi * float64(i) / qFFT
		w := 0.35875 - 0.48829*math.Cos(a) + 0.14128*math.Cos(2*a) - 0.01168*math.Cos(3*a)
		z[i] = complex(v*w, 0)
	}
	qFFTInPlace(z)
	p := make([]float64, qFFT/2)
	for i := range p {
		a := cmplx.Abs(z[i])
		p[i] = a * a
	}
	return p
}

func qFFTInPlace(z []complex128) {
	n := len(z)
	for i, j := 1, 0; i < n; i++ {
		bit := n >> 1
		for ; j&bit != 0; bit >>= 1 {
			j ^= bit
		}
		j ^= bit
		if i < j {
			z[i], z[j] = z[j], z[i]
		}
	}
	for size := 2; size <= n; size <<= 1 {
		w := cmplx.Exp(complex(0, -2*math.Pi/float64(size)))
		for start := 0; start < n; start += size {
			wk := complex(1, 0)
			for k := 0; k < size/2; k++ {
				a, b := z[start+k], z[start+k+size/2]*wk
				z[start+k], z[start+k+size/2] = a+b, a-b
				wk *= w
			}
		}
	}
}

// qDistortionDb is the power at the products a nonlinearity makes from the
// stimulus, relative to the stimulus, in dB: every multiple of grid Hz up to
// 10kHz that is not itself a tone, each read over +-10Hz (which takes in the
// 4Hz syllable's sidebands) less the noise floor measured beside it. The noise
// is subtracted so that the S16 floor of a quiet output, which is level-bound
// and not a fault, does not read as distortion.
func qDistortionDb(p []float64, tones []float64, grid float64) float64 {
	bin := float64(qFs) / qFFT
	band := func(lo, hi float64) (sum float64, n int) {
		for i := int(math.Ceil(lo / bin)); float64(i)*bin <= hi && i < len(p); i++ {
			sum += p[i]
			n++
		}
		return
	}
	var sig float64
	for _, t := range tones {
		s, _ := band(t-10, t+10)
		sig += s
	}
	var dist float64
	for f := grid; f <= 10000; f += grid {
		isTone := false
		for _, t := range tones {
			if math.Abs(f-t) < 1 {
				isTone = true
			}
		}
		if isTone {
			continue
		}
		s, n := band(f-10, f+10)
		// The floor: the mean of the gaps either side, half a grid step
		// away, where no product lands.
		lo, nl := band(f-grid/2-5, f-grid/2+5)
		hi, nh := band(f+grid/2-5, f+grid/2+5)
		floor := (lo + hi) / float64(nl+nh)
		dist += math.Max(s-floor*float64(n), 0)
	}
	return 10 * math.Log10(math.Max(dist, 1e-30)/sig)
}

func qRMSDbfs(y []float64) float64 {
	seg := y[len(y)-qFFT:]
	var s float64
	for _, v := range seg {
		s += v * v
	}
	return 20 * math.Log10(math.Max(math.Sqrt(s/float64(len(seg))), 1e-9)/32767)
}

// qToneDb is the power within 3 bins of f, in dB, for the response.
func qToneDb(p []float64, f float64) float64 {
	c := int(math.Round(f * qFFT / qFs))
	var s float64
	for i := c - 3; i <= c+3; i++ {
		s += p[i]
	}
	return 10 * math.Log10(s)
}

func qRound(v float64) float64 { return math.Round(v*100) / 100 }

func qMeasure(t *testing.T, sp board.Speaker, plugged bool) qPath {
	r := qPath{Loudness: map[string]float64{}, Dist: map[string]float64{}, THD: map[string]float64{}}
	prog, syl, sine := qSignal("programme"), qSignal("syllable"), qSignal("sine1k")
	progIn := qSpectrum(prog)
	for _, lvl := range qLevels {
		k := fmt.Sprint(lvl)
		y := qRender(t, sp, plugged, lvl, prog)
		r.Loudness[k] = qRound(qRMSDbfs(y))
		if lvl == qRefLvl {
			py := qSpectrum(y)
			for _, f := range qTones {
				r.Response = append(r.Response, qRound(qToneDb(py, f)-qToneDb(progIn, f)))
			}
		}
		r.Dist[k] = qRound(qDistortionDb(qSpectrum(qRender(t, sp, plugged, lvl, syl)), []float64{200, 300}, 100))
		r.THD[k] = qRound(qDistortionDb(qSpectrum(qRender(t, sp, plugged, lvl, sine)), []float64{1000}, 1000))
	}
	return r
}

func TestOutputQuality(t *testing.T) {
	got := map[string]qPath{}
	for _, b := range board.Known {
		for _, plugged := range []bool{false, true} {
			name := b.ID + "/speaker"
			if plugged {
				name = b.ID + "/jack"
			}
			got[name] = qMeasure(t, b.Hardware.Speaker, plugged)
		}
	}

	// Bounds first: they hold even when the baseline is being rewritten.
	for name, r := range got {
		prev := math.Inf(-1)
		for _, lvl := range qLevels {
			k := fmt.Sprint(lvl)
			if r.Loudness[k] >= qBoundFloorDbfs && r.Dist[k] > qDistMaxDb {
				t.Errorf("%s level %d: syllable distortion %.1fdB, bound %d", name, lvl, r.Dist[k], qDistMaxDb)
			}
			if r.Loudness[k] >= qBoundFloorDbfs && r.THD[k] > qTHDMaxDb {
				t.Errorf("%s level %d: 1kHz THD %.1fdB, bound %d", name, lvl, r.THD[k], qTHDMaxDb)
			}
			if r.Loudness[k] < prev-qTolDb {
				t.Errorf("%s level %d: quieter than the level below (%.2f < %.2f dBFS)", name, lvl, r.Loudness[k], prev)
			}
			prev = r.Loudness[k]
		}
	}

	path := filepath.Join("testdata", "quality_baseline.json")
	if *updateQuality {
		b, err := json.MarshalIndent(got, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("wrote %s", path)
		return
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run with -update-quality to create it)", err)
	}
	var want map[string]qPath
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatal(err)
	}

	names := make([]string, 0, len(got))
	for n := range got {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, name := range names {
		g, w := got[name], want[name]
		if w.Loudness == nil {
			t.Errorf("%s: not in the baseline (run with -update-quality)", name)
			continue
		}
		for _, lvl := range qLevels {
			k := fmt.Sprint(lvl)
			for _, m := range []struct {
				what      string
				got, want float64
			}{
				{"programme loudness dBFS", g.Loudness[k], w.Loudness[k]},
				{"syllable distortion dB", g.Dist[k], w.Dist[k]},
				{"1kHz THD dB", g.THD[k], w.THD[k]},
			} {
				if math.Abs(m.got-m.want) > qTolDb {
					t.Errorf("%s level %d: %s %.2f, baseline %.2f", name, lvl, m.what, m.got, m.want)
				}
			}
		}
		for i, f := range qTones {
			if i < len(w.Response) && math.Abs(g.Response[i]-w.Response[i]) > qTolDb {
				t.Errorf("%s: response at %.0fHz %.2fdB, baseline %.2fdB", name, f, g.Response[i], w.Response[i])
			}
		}
	}
	for name := range want {
		if _, ok := got[name]; !ok {
			t.Errorf("%s: in the baseline but no longer measured", name)
		}
	}
}
