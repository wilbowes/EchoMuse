// vad_probe — run Silero VAD on the device and say whether it is correct and
// what it costs, the question that decides whether the device's RMS speech
// gate can be replaced by a model.
//
// Phase 1 scores testdata/vad_fixture against Python's probabilities. Phase 2
// paces 80ms frames at real time and reports process CPU from getrusage, for
// the reason oww_probe does: ORT's thread pool can cost more between frames
// than inside them.
//
// The model must be the typed-field rewrite (controller/tools/silero_typed.py),
// not openwakeword's file as shipped:
// ORT 1.19 on armv7 dies with SIGBUS (BUS_ADRALN) inside CreateSession on
// tensors stored as raw_data, which protobuf leaves at arbitrary offsets, and
// Android's debuggerd then crashes dumping it, leaving the process stopped (T)
// with an empty tombstone. The rewrite is bit-identical in output.
//
// Build with device/tools/build_tools.sh. Deploy the binary, silero_vad.onnx,
// vad_fixture.pcm/.json and libonnxruntime.so, then:
//
//	vad_probe -lib /data/local/tmp/libonnxruntime.so -model /data/local/tmp/silero_vad.onnx \
//	    -fixture /data/local/tmp -seconds 60
package main

import (
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"syscall"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword/ort"
)

const frame = 1280 // 80ms at 16kHz

func main() {
	var (
		lib     = flag.String("lib", "/data/local/tmp/libonnxruntime.so", "path to libonnxruntime.so")
		model   = flag.String("model", "/data/local/tmp/silero_vad.onnx", "path to silero_vad.onnx")
		fxDir   = flag.String("fixture", "/data/local/tmp", "directory holding vad_fixture.pcm and .json")
		seconds = flag.Int("seconds", 60, "paced measurement length (0 to skip)")
		threads = flag.Int("threads", 1, "intra-op threads")
		xnnpack = flag.Bool("xnnpack", true, "use the XNNPACK execution provider")
	)
	flag.Parse()
	if err := run(*lib, *model, *fxDir, *seconds,
		ort.Options{Threads: *threads, XNNPACK: *xnnpack}); err != nil {
		fmt.Fprintf(os.Stderr, "vad_probe: %v\n", err)
		os.Exit(1)
	}
}

func run(lib, model, fxDir string, seconds int, opts ort.Options) error {
	audio, want, err := load(fxDir)
	if err != nil {
		return err
	}
	rt, err := ort.Open(lib)
	if err != nil {
		return err
	}
	fmt.Printf("opened    onnxruntime %s; loading %s\n", rt.Version(), model)
	v, err := rt.NewVAD(model, opts)
	if err != nil {
		return err
	}
	defer v.Close()
	fmt.Printf("runtime   onnxruntime %s, threads=%d xnnpack=%v(requested %v)\n",
		rt.Version(), opts.Threads, v.XNNPACKActive(), opts.XNNPACK)

	fmt.Println("\n== phase 1: does it reproduce Python? ==")
	var worst float64
	st := v.Stream()
	for i, w := range want {
		p, err := st.Prob(audio[i*frame : (i+1)*frame])
		if err != nil {
			return err
		}
		worst = math.Max(worst, math.Abs(float64(p-w)))
	}
	ok := worst <= 1e-3
	fmt.Printf("  %d frames, worst difference %.2g — %s\n", len(want), worst, map[bool]string{true: "ok", false: "FAIL"}[ok])

	if seconds > 0 {
		paced(v, audio, seconds)
	}
	if !ok {
		return fmt.Errorf("VERDICT: this device does NOT reproduce Python")
	}
	return nil
}

func paced(v *ort.VAD, audio []float32, seconds int) {
	fmt.Printf("\n== phase 2: cost at 12.5 frames/s, %ds ==\n", seconds)
	frames := seconds * 16000 / frame
	n := len(audio) / frame
	interval := 80 * time.Millisecond

	var ru0, ru1 syscall.Rusage
	_ = syscall.Getrusage(syscall.RUSAGE_SELF, &ru0)
	lat := make([]time.Duration, 0, frames)
	start := time.Now()
	st := v.Stream()
	for i := 0; i < frames; i++ {
		t0 := time.Now()
		if _, err := st.Prob(audio[(i%n)*frame : (i%n+1)*frame]); err != nil {
			fmt.Printf("  error: %v\n", err)
			return
		}
		lat = append(lat, time.Since(t0))
		if d := time.Until(start.Add(time.Duration(i+1) * interval)); d > 0 {
			time.Sleep(d)
		}
	}
	wall := time.Since(start)
	_ = syscall.Getrusage(syscall.RUSAGE_SELF, &ru1)
	cpu := time.Duration(ru1.Utime.Nano()-ru0.Utime.Nano()) + time.Duration(ru1.Stime.Nano()-ru0.Stime.Nano())

	sort.Slice(lat, func(i, j int) bool { return lat[i] < lat[j] })
	q := func(f float64) float64 { return float64(lat[int(f*float64(len(lat)-1))].Microseconds()) / 1000 }
	fmt.Printf("  frames %d over %.1fs\n", frames, wall.Seconds())
	fmt.Printf("  latency p50 %.2fms  p99 %.2fms  max %.2fms (budget 80ms)\n", q(0.5), q(0.99), q(1))
	fmt.Printf("  CPU     %.1f%% of one core\n", 100*cpu.Seconds()/wall.Seconds())
}

func load(dir string) ([]float32, []float32, error) {
	raw, err := os.ReadFile(filepath.Join(dir, "vad_fixture.pcm"))
	if err != nil {
		return nil, nil, err
	}
	audio := make([]float32, len(raw)/2)
	for i := range audio {
		audio[i] = float32(int16(binary.LittleEndian.Uint16(raw[2*i:]))) / 32767
	}
	js, err := os.ReadFile(filepath.Join(dir, "vad_fixture.json"))
	if err != nil {
		return nil, nil, err
	}
	var g struct{ Probs []float32 }
	if err := json.Unmarshal(js, &g); err != nil {
		return nil, nil, err
	}
	return audio, g.Probs, nil
}
