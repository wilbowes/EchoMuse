package outchain

import (
	"compress/gzip"
	"encoding/binary"
	"encoding/json"
	"io"
	"math"
	"os"
	"path/filepath"
	"testing"
)

// vectorParams is the manifest's parameter object, as em_db names the keys.
type vectorParams struct {
	Bands            []float64 `json:"bands"`
	Loudness         bool      `json:"loudness"`
	GuardEnabled     bool      `json:"guardEnabled"`
	GuardDb          float64   `json:"guardDb"`
	LimiterEnabled   bool      `json:"limiterEnabled"`
	LimiterThreshold float64   `json:"limiterThreshold"`
	LimiterRelease   float64   `json:"limiterRelease"`
}

func (v vectorParams) params() Params {
	p := Params{
		Loudness:           v.Loudness,
		GuardEnabled:       v.GuardEnabled,
		GuardDb:            v.GuardDb,
		LimiterEnabled:     v.LimiterEnabled,
		LimiterThresholdDb: v.LimiterThreshold,
		LimiterReleaseMs:   v.LimiterRelease,
	}
	copy(p.Bands[:], v.Bands)
	return p
}

type vectorCase struct {
	Name       string `json:"name"`
	Chunk      int    `json:"chunk"`
	SampleRate int    `json:"sampleRate"`
	Chunks     int    `json:"chunks"`
	Schedule   [][2]json.RawMessage
	Stats      struct {
		GuardReductionDb   float64 `json:"guardReductionDb"`
		LimiterReductionDb float64 `json:"limiterReductionDb"`
		Clipped            uint64  `json:"clipped"`
		ClippedBypassed    uint64  `json:"clippedBypassed"`
	} `json:"stats"`
}

func readS16(t *testing.T, path string) []int16 {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := io.ReadAll(gz)
	if err != nil {
		t.Fatal(err)
	}
	out := make([]int16, len(raw)/2)
	for i := range out {
		out[i] = int16(binary.LittleEndian.Uint16(raw[i*2:]))
	}
	return out
}

func stereo(mono []int16) []byte {
	buf := make([]byte, len(mono)*4)
	for i, s := range mono {
		binary.LittleEndian.PutUint16(buf[i*4:], uint16(s))
		binary.LittleEndian.PutUint16(buf[i*4+2:], uint16(s))
	}
	return buf
}

// TestMatchesControllerChain holds the port to em_eq/em_mbc/em_limiter on
// vectors the Python generated (testdata/gen_vectors.py), period by period,
// with every parameter change applied where the device would apply it.
//
// Tolerance is ONE LSB on a handful of samples, and it exists for one
// reason: numpy vectorises the release recursions as a sheared running
// minimum and computes 10**x its own way, so the float64 gain differs from
// ours in the last bits, and a sample sitting on an integer boundary can
// truncate either side of it. Anything larger is a behaviour difference.
func TestMatchesControllerChain(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var cases []vectorCase
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("no vectors")
	}

	for _, vc := range cases {
		t.Run(vc.Name, func(t *testing.T) {
			in := readS16(t, filepath.Join("testdata", vc.Name+".in.s16.gz"))
			want := readS16(t, filepath.Join("testdata", vc.Name+".out.s16.gz"))
			if len(in) != vc.Chunks*vc.Chunk || len(want) != len(in) {
				t.Fatalf("vector lengths: in=%d want=%d, expected %d",
					len(in), len(want), vc.Chunks*vc.Chunk)
			}

			sched := map[int]Params{}
			for _, e := range vc.Schedule {
				var at int
				var vp vectorParams
				if err := json.Unmarshal(e[0], &at); err != nil {
					t.Fatal(err)
				}
				if err := json.Unmarshal(e[1], &vp); err != nil {
					t.Fatal(err)
				}
				sched[at] = vp.params()
			}

			c := New(vc.SampleRate)
			c.SetActive(true)
			got := make([]int16, 0, len(in))
			for k := 0; k < vc.Chunks; k++ {
				if p, ok := sched[k]; ok {
					c.SetParams(p)
				}
				buf := stereo(in[k*vc.Chunk : (k+1)*vc.Chunk])
				c.Process(buf)
				for i := 0; i < len(buf); i += 4 {
					l := int16(binary.LittleEndian.Uint16(buf[i:]))
					r := int16(binary.LittleEndian.Uint16(buf[i+2:]))
					if l != r {
						t.Fatalf("chunk %d frame %d: L=%d R=%d", k, i/4, l, r)
					}
					got = append(got, l)
				}
			}

			off, worst := 0, 0
			for i := range want {
				d := int(got[i]) - int(want[i])
				if d != 0 {
					off++
				}
				if d < 0 {
					d = -d
				}
				if d > worst {
					worst = d
					if d > 1 {
						t.Errorf("sample %d (chunk %d): got %d want %d",
							i, i/vc.Chunk, got[i], want[i])
					}
				}
			}
			// One in a thousand is generous for last-bit rounding and far
			// short of anything systematic.
			if off > len(want)/1000 {
				t.Errorf("%d of %d samples differ (worst %d LSB)", off, len(want), worst)
			}
			t.Logf("%d of %d samples differ by 1 LSB", off, len(want))

			st := c.TakeStats()
			if math.Abs(st.GuardReductionDb-vc.Stats.GuardReductionDb) > 1e-6 {
				t.Errorf("guard reduction %.6f, want %.6f", st.GuardReductionDb, vc.Stats.GuardReductionDb)
			}
			if math.Abs(st.LimiterReductionDb-vc.Stats.LimiterReductionDb) > 1e-6 {
				t.Errorf("limiter reduction %.6f, want %.6f", st.LimiterReductionDb, vc.Stats.LimiterReductionDb)
			}
			if st.Clipped != vc.Stats.Clipped || st.ClippedBypassed != vc.Stats.ClippedBypassed {
				t.Errorf("clipped %d/%d bypassed, want %d/%d",
					st.Clipped, st.ClippedBypassed, vc.Stats.Clipped, vc.Stats.ClippedBypassed)
			}
		})
	}
}

func TestWidePeriodKeepsChainStateInNormalisedDomain(t *testing.T) {
	reference, switched := New(48000), New(48000)
	reference.SetActive(true)
	switched.SetActive(true)

	for chunk := 0; chunk < 3; chunk++ {
		mono := make([]int16, 512)
		for i := range mono {
			n := chunk*len(mono) + i
			mono[i] = int16(12000 * math.Sin(2*math.Pi*80*float64(n)/48000))
		}
		want := stereo(mono)
		reference.Process(want)

		if chunk == 1 {
			const scale = 4.0
			wide := make([]float64, len(mono)*2)
			scales := make([]float64, len(mono))
			for i, sample := range mono {
				wide[i*2], wide[i*2+1] = float64(sample)*scale, float64(sample)*scale
				scales[i] = scale
			}
			switched.ProcessFloat(wide, scales)
			for i := range mono {
				got := wide[i*2] / scale
				expected := float64(int16(binary.LittleEndian.Uint16(want[i*4:])))
				if math.Abs(got-expected) > 1 {
					t.Fatalf("wide frame %d: normalised %.3f, want %.3f", i, got, expected)
				}
			}
			continue
		}

		got := stereo(mono)
		switched.Process(got)
		if string(got) != string(want) {
			t.Fatalf("ordinary chunk %d changed across wide-period transition", chunk)
		}
	}
}

// BenchmarkPeriod is one 2048-frame period through the whole chain, shaped
// EQ plus speech boost, guard and limiter all on — the most work it can do.
// A period is 42.7ms of audio; ns/op against that is the realtime share.
func BenchmarkPeriod(b *testing.B) {
	p := DefaultParams()
	p.Bands = [NumBands]float64{6, 3, 0, -2, 0, 2, 4, 1}
	p.Loudness = true
	benchPeriod(b, p)
}

// BenchmarkPeriodDefaults is the chain as the fleet runs it: flat EQ, guard
// and limiter on — the common case, where the EQ costs nothing.
func BenchmarkPeriodDefaults(b *testing.B) { benchPeriod(b, DefaultParams()) }

func benchPeriod(b *testing.B, p Params) {
	c := New(48000)
	c.SetActive(true)
	c.SetParams(p)
	mono := make([]int16, 2048)
	for i := range mono {
		mono[i] = int16(20000 * math.Sin(2*math.Pi*80*float64(i)/48000))
	}
	src := stereo(mono)
	buf := make([]byte, len(src))
	b.SetBytes(int64(len(src)))
	for i := 0; i < b.N; i++ {
		copy(buf, src)
		c.Process(buf)
	}
}
