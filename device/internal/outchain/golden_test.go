package outchain

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"flag"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// The mono output as it was before per-channel processing existed (#273),
// pinned by hash: every vector through each of the three entry points, with
// its schedule applied where TestMatchesControllerChain applies it. The
// vectors themselves allow 1 LSB against the Python; this allows nothing, so
// a change to the chain that moves a single sample of the mono path fails
// here even when it stays inside that tolerance.
//
// Generated once, before the stereo path was written, and not to be
// regenerated to make a change pass: a moved hash means the mono output
// moved. amd64 only, since another architecture may fuse multiply-adds and
// round differently.
var updateGolden = flag.Bool("update-golden", false, "rewrite testdata/golden_main.json")

type goldenHashes struct {
	Process      string `json:"process"`
	ProcessFloat string `json:"processFloat"`
	ProcessWide  string `json:"processWide"`
}

// goldenSchedule reads a vector's parameter schedule, keyed by period.
func goldenSchedule(t *testing.T, vc vectorCase) map[int]Params {
	t.Helper()
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
	return sched
}

// goldenScale is the full-scale multiplier ProcessFloat sees at frame i of
// period k: it moves between periods and within one, as a boosted response
// does.
func goldenScale(k, i int) float64 {
	return []float64{1, 2, 0.5, 4}[k&3] * (1 + 0.25*float64(i&3))
}

func goldenRun(t *testing.T, vc vectorCase) goldenHashes {
	t.Helper()
	in := readS16(t, filepath.Join("testdata", vc.Name+".in.s16.gz"))
	sched := goldenSchedule(t, vc)

	proc, flt, wide := New(vc.SampleRate), New(vc.SampleRate), New(vc.SampleRate)
	for _, c := range []*Chain{proc, flt, wide} {
		c.SetActive(true)
	}
	hp, hf, hw := sha256.New(), sha256.New(), sha256.New()
	var word [8]byte
	for k := 0; k < vc.Chunks; k++ {
		if p, ok := sched[k]; ok {
			proc.SetParams(p)
			flt.SetParams(p)
			wide.SetParams(p)
		}
		mono := in[k*vc.Chunk : (k+1)*vc.Chunk]

		buf := stereo(mono)
		proc.Process(buf)
		hp.Write(buf)

		fb := make([]float64, len(mono)*2)
		scales := make([]float64, len(mono))
		for i, s := range mono {
			scales[i] = goldenScale(k, i)
			fb[i*2], fb[i*2+1] = float64(s)*scales[i], float64(s)*scales[i]
		}
		flt.ProcessFloat(fb, scales)
		for _, x := range fb {
			binary.LittleEndian.PutUint64(word[:], math.Float64bits(x))
			hf.Write(word[:])
		}

		// The true level, up to 1.75x over the S16 sample, as the stock law
		// drives it near the top.
		wb := make([]float64, len(mono)*2)
		g := 1 + 0.25*float64(k&3)
		for i, s := range mono {
			wb[i*2], wb[i*2+1] = float64(s)*g, float64(s)*g
		}
		out := make([]byte, len(mono)*4)
		wide.ProcessWide(wb, out)
		hw.Write(out)
	}
	return goldenHashes{
		Process:      hex.EncodeToString(hp.Sum(nil)),
		ProcessFloat: hex.EncodeToString(hf.Sum(nil)),
		ProcessWide:  hex.EncodeToString(hw.Sum(nil)),
	}
}

func TestMonoOutputMatchesGolden(t *testing.T) {
	if runtime.GOARCH != "amd64" {
		t.Skipf("golden hashes are amd64's; %s may round differently", runtime.GOARCH)
	}
	raw, err := os.ReadFile(filepath.Join("testdata", "vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var cases []vectorCase
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	got := map[string]goldenHashes{}
	for _, vc := range cases {
		got[vc.Name] = goldenRun(t, vc)
	}

	path := filepath.Join("testdata", "golden_main.json")
	if *updateGolden {
		b, err := json.MarshalIndent(got, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("wrote %s", path)
		return
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var want map[string]goldenHashes
	if err := json.Unmarshal(b, &want); err != nil {
		t.Fatal(err)
	}
	if len(want) != len(got) {
		t.Errorf("%d vectors hashed, golden has %d", len(got), len(want))
	}
	for name, g := range got {
		w, ok := want[name]
		switch {
		case !ok:
			t.Errorf("%s: not in the golden file", name)
		case g != w:
			if g.Process != w.Process {
				t.Errorf("%s: Process output moved", name)
			}
			if g.ProcessFloat != w.ProcessFloat {
				t.Errorf("%s: ProcessFloat output moved", name)
			}
			if g.ProcessWide != w.ProcessWide {
				t.Errorf("%s: ProcessWide output moved", name)
			}
		}
	}
}
