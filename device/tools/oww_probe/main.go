// oww_probe — run the on-device wake word pipeline on the device and say
// whether it is correct and what it costs.
//
// It answers the two questions the host tests cannot:
//
//  1. Does the ONNX Runtime binding actually work on this hardware? The probe
//     runs fixture.Verify — the SAME comparison the host test runs, against
//     the same golden capture of openWakeWord's Python output — so a pass here
//     means the real pipeline reproduces Python on ARM, not merely on x86.
//
//  2. What does it cost in the shape it will really run? Not flat-out latency,
//     which is the misleading number: the wake stream is duty-cycled at 12.5Hz,
//     and ORT's thread pool can burn several times the inference cost
//     spin-waiting in the gaps. So the second phase paces frames at real time
//     and reports process CPU from getrusage. That also captures what a C
//     benchmark cannot — cgo call overhead and Go GC pressure from the
//     buffering.
//
// Nothing here touches the microphone, the LEDs, or the running server. It
// reads three model files, a fixture, and the ONNX Runtime shared library, and
// prints. It is safe to run on a live device, though phase 2 competes for CPU
// with whatever else is running, which is the point.
//
// Build with device/tools/build_tools.sh (it needs the parent module, so unlike
// the other tools it is not a standalone module). Deploy the binary, the three
// .onnx files, ort_fixture.bin and libonnxruntime.so, then:
//
//	oww_probe -lib /data/local/tmp/libonnxruntime.so -models /data/local/tmp \
//	    -classifier /data/local/tmp/hey_mycroft_v0.1.onnx \
//	    -fixture /data/local/tmp/ort_fixture.bin -seconds 30
//
// Exit status is 0 only if every stage matched Python.
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"syscall"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
	"github.com/wilbowes/EchoMuse/internal/wakeword/fixture"
	"github.com/wilbowes/EchoMuse/internal/wakeword/ort"
)

func main() {
	var (
		lib      = flag.String("lib", "/data/local/tmp/libonnxruntime.so", "path to libonnxruntime.so")
		modelDir = flag.String("models", "/data/local/tmp", "directory holding melspectrogram.onnx and embedding_model.onnx")
		clsPath  = flag.String("classifier", "", "path to the wake word classifier .onnx (required)")
		fxPath   = flag.String("fixture", "/data/local/tmp/ort_fixture.bin", "path to ort_fixture.bin")
		seconds  = flag.Int("seconds", 20, "how long to run the paced CPU measurement (0 to skip)")
		threads  = flag.Int("threads", 1, "intra-op threads")
		xnnpack  = flag.Bool("xnnpack", true, "use the XNNPACK execution provider")
		spinning = flag.Bool("spinning", false, "leave ORT's thread-pool spin-wait enabled (the costly default)")
	)
	flag.Parse()

	if *clsPath == "" {
		fmt.Fprintln(os.Stderr, "oww_probe: -classifier is required")
		os.Exit(2)
	}

	if err := run(*lib, *modelDir, *clsPath, *fxPath, *seconds,
		ort.Options{Threads: *threads, XNNPACK: *xnnpack, AllowSpinning: *spinning}); err != nil {
		fmt.Fprintf(os.Stderr, "oww_probe: %v\n", err)
		os.Exit(1)
	}
}

func run(lib, modelDir, clsPath, fxPath string, seconds int, opts ort.Options) error {
	fx, err := fixture.LoadORT(fxPath)
	if err != nil {
		return err
	}
	fmt.Printf("fixture   %d chunks, %.2fs of audio\n",
		len(fx.Records), float64(len(fx.Audio))/wakeword.SampleRate)

	rt, err := ort.Open(lib)
	if err != nil {
		return err
	}
	inf, err := rt.NewInferer(ort.Models{
		Melspec:    filepath.Join(modelDir, "melspectrogram.onnx"),
		Embedding:  filepath.Join(modelDir, "embedding_model.onnx"),
		Classifier: clsPath,
	}, opts)
	if err != nil {
		return err
	}
	defer inf.Close()

	fmt.Printf("runtime   onnxruntime %s\n", rt.Version())
	fmt.Printf("session   threads=%d xnnpack=%v(requested %v) spinning=%v\n",
		opts.Threads, inf.XNNPACKActive(), opts.XNNPACK, opts.AllowSpinning)
	if opts.XNNPACK && !inf.XNNPACKActive() {
		// Not fatal, but it changes the CPU numbers below by ~1.5x, so it must
		// not go unnoticed in a log someone reads later.
		fmt.Println("WARNING   XNNPACK was requested but is unavailable; using the CPU provider")
	}

	fmt.Println("\n== phase 1: does it reproduce Python? ==")
	rep := fixture.Verify(inf, fx)
	fmt.Print(rep.Summary())

	// The probe block: on synthetic noise every score is 0.0000, so the audio
	// path alone would also pass with a classifier stuck at zero.
	probeOk := true
	emb, err := inf.Embed(fx.EmbProbeIn)
	if err != nil {
		return fmt.Errorf("embedding probe: %w", err)
	}
	if d := fixture.Compare(emb, fx.EmbProbeOut); !d.Ok() {
		fmt.Printf("  probe      FAIL embedding: %d values differ, worst %.3g: %v\n", d.N, d.Worst, d.Examples)
		probeOk = false
	}
	score, err := inf.Classify(fx.ClsProbeIn)
	if err != nil {
		return fmt.Errorf("classifier probe: %w", err)
	}
	if !fixture.CloseEnough(score, fx.ClsProbeOut) {
		fmt.Printf("  probe      FAIL classifier: %v, Python gave %v\n", score, fx.ClsProbeOut)
		probeOk = false
	}
	if probeOk {
		fmt.Printf("  probe      ok                       classifier %.6f matches Python\n", score)
	}

	if seconds > 0 {
		if err := paced(inf, fx, seconds); err != nil {
			return err
		}
	}

	if !rep.Ok() || !probeOk {
		return fmt.Errorf("VERDICT: this device does NOT reproduce Python")
	}
	fmt.Println("\nVERDICT: pass — the pipeline reproduces Python on this device")
	return nil
}

// paced runs the pipeline at the real duty cycle and reports process CPU.
//
// The measurement that matters is CPU as a percentage of one core, because the
// wake stream runs 24/7 on top of an existing ~18-20% mic-pipeline baseline.
// Per-frame latency is reported too, but only as a headroom check: it has 80ms
// to fit inside, and being fast is worth nothing if it costs a whole core.
func paced(inf *ort.Inferer, fx *fixture.ORT, seconds int) error {
	fmt.Printf("\n== phase 2: cost at the real 12.5Hz duty cycle, %ds ==\n", seconds)

	d := wakeword.New(inf)
	frames := seconds * wakeword.SampleRate / wakeword.ChunkSamples
	interval := time.Duration(wakeword.ChunkSamples) * time.Second / wakeword.SampleRate

	var ru0, ru1 syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru0); err != nil {
		return fmt.Errorf("getrusage: %w", err)
	}

	lat := make([]time.Duration, 0, frames)
	var late int
	start := time.Now()
	for i := 0; i < frames; i++ {
		// Real audio, looped. Silence would be unrepresentative: these are
		// convolutions over whatever is in the buffer, and a degenerate input
		// can take a different amount of time.
		chunk := fx.Chunk(i % len(fx.Records))

		t0 := time.Now()
		if _, err := d.Push(chunk); err != nil {
			return fmt.Errorf("frame %d: push: %w", i, err)
		}
		if d.Ready() {
			if _, err := d.Score(); err != nil {
				return fmt.Errorf("frame %d: score: %w", i, err)
			}
		}
		lat = append(lat, time.Since(t0))

		// Pace against the start, not the previous frame, so the schedule
		// cannot drift; a frame that overruns its slot is counted rather than
		// silently absorbed.
		next := start.Add(time.Duration(i+1) * interval)
		if wait := time.Until(next); wait > 0 {
			time.Sleep(wait)
		} else {
			late++
		}
	}
	wall := time.Since(start)

	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru1); err != nil {
		return fmt.Errorf("getrusage: %w", err)
	}
	cpu := tv(ru1.Utime) + tv(ru1.Stime) - tv(ru0.Utime) - tv(ru0.Stime)

	sort.Slice(lat, func(i, j int) bool { return lat[i] < lat[j] })
	p := func(q float64) time.Duration { return lat[int(float64(len(lat)-1)*q)] }

	fmt.Printf("  frames     %d over %.1fs (%d overran their 80ms slot)\n", frames, wall.Seconds(), late)
	fmt.Printf("  latency    p50 %.1fms  p95 %.1fms  max %.1fms  (budget %.0fms)\n",
		ms(p(0.50)), ms(p(0.95)), ms(lat[len(lat)-1]), ms(interval))
	fmt.Printf("  CPU        %.1f%% of one core  (%.2fs of CPU in %.1fs wall)\n",
		cpu/wall.Seconds()*100, cpu, wall.Seconds())
	fmt.Printf("  RSS        %d KB peak\n", ru1.Maxrss)
	return nil
}

func tv(t syscall.Timeval) float64 { return float64(t.Sec) + float64(t.Usec)/1e6 }
func ms(d time.Duration) float64   { return float64(d) / float64(time.Millisecond) }
