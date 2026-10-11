// aec_replay runs a bench raw capture (rawtap_bench.go) through the device's
// own mic front end on the host: beamformer extraction with the mic gain,
// the ch8 hardware reference, and the speex
// canceller exactly as the firmware configures it. Every run starts cold,
// which is the case the harness exists to measure.
//
// Writes, 16kHz mono S16 WAV:
//
//	mic.wav    near end before cancellation (what the wake model sees with AEC off)
//	ref.wav    the reference as the canceller receives it
//	speex.wav  near end after cancellation
//
// Usage (host build, in the compiler image with GOARCH=amd64):
//
//	go run ./tools/aec_replay -in C1.s24 -out out/C1
//
// A turn locks to one microphone and the next turn to another. -mics replays
// that (seconds:channel pairs), with one filter following the microphone in
// use, or with -per-mic one per microphone as the firmware runs it (#814):
//
//	go run ./tools/aec_replay -in C1.s24 -out out/C1 -mics 0:6,12.6:2,30:6,32:3 -per-mic
//
// -tail sets the software tap's length only. On the hardware reference, which
// is all this tool uses, the canceller keeps its own 64ms.
//
// There is no playback-level option: since #638 the volume is applied in
// software before the loopback, so the reference already carries it. A
// capture from firmware before that has a pre-volume reference and does not
// replay faithfully here.
package main

import (
	"encoding/binary"
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"strings"

	"github.com/wilbowes/EchoMuse/internal/aec"
	"github.com/wilbowes/EchoMuse/internal/beamformer"
)

const (
	frameBytes = 27   // 9 channels × S24_3LE
	batch      = 2560 // frames per ALSA read on the device (160ms)
	rate       = 16000
)

func main() {
	in := flag.String("in", "", "raw capture (.s24)")
	out := flag.String("out", "", "output directory")
	tail := flag.Int("tail", 300, "speex filter tail for the software tap, ms; no effect here, the hardware reference keeps 64")
	gainDb := flag.Float64("gain", 24, "mic gain, dB (micGainDb)")
	load := flag.String("load", "", "start from a saved echo path (aec.ExportState) instead of cold")
	save := flag.String("save", "", "write the echo path learnt by the end of this run")
	mics := flag.String("mics", "", "microphone in use over time, as seconds:channel pairs, e.g. 0:6,12.6:2,30:6,32:3 (default: ch6 throughout)")
	perMic := flag.Bool("per-mic", false, "with -mics: cancel every microphone, as the firmware does, instead of one filter following the microphone in use")
	prime := flag.Bool("prime", false, "converge on one full pass first and write the second as speex_primed.wav: the ceiling, not a real run")
	flag.Parse()
	if *in == "" || *out == "" {
		flag.Usage()
		os.Exit(2)
	}
	raw, err := os.ReadFile(*in)
	if err != nil {
		log.Fatal(err)
	}
	if err := os.MkdirAll(*out, 0o755); err != nil {
		log.Fatal(err)
	}

	bf := beamformer.New()
	c := aec.New()
	c.SetParams(true, 0, *tail)
	c.SetHardwareRef(true)
	gain := math.Pow(10, *gainDb/20)
	if *load != "" {
		b, err := os.ReadFile(*load)
		if err != nil {
			log.Fatal(err)
		}
		if err := c.ImportState(b); err != nil {
			log.Fatal(err)
		}
	}

	var mic, ref, speex []byte
	step := batch * frameBytes
	if *prime {
		pb, pc := beamformer.New(), aec.New()
		pc.SetParams(true, 0, *tail)
		pc.SetHardwareRef(true)
		var primed []byte
		for pass := 0; pass < 2; pass++ {
			for off := 0; off+step <= len(raw); off += step {
				period := raw[off : off+step]
				m, _ := pb.Process(period, -1, gain)
				o := pc.ProcessWithRef(m, pb.EchoRef(period))
				if pass == 1 {
					primed = append(primed, o...)
				}
			}
		}
		if err := writeWAV(filepath.Join(*out, "speex_primed.wav"), primed); err != nil {
			log.Fatal(err)
		}
	}
	plan, err := parseMics(*mics)
	if err != nil {
		log.Fatal(err)
	}
	for off := 0; off+step <= len(raw); off += step {
		period := raw[off : off+step]
		r := bf.EchoRef(period)
		ref = append(ref, r...)
		if plan == nil {
			m, _ := bf.Process(period, -1, gain) // unlocked: ch6, as the wake stream
			mic = append(mic, m...)
			speex = append(speex, c.ProcessWithRef(m, r)...)
			continue
		}
		// A turn locks to a microphone and the next one to another; -mics
		// replays that, with one filter or with one per microphone (#814).
		active := plan.at(float64(off/frameBytes) / rate)
		all := bf.MicChannels(period, gain, -1, nil)
		mic = append(mic, all[active]...)
		if *perMic {
			speex = append(speex, c.ProcessMicsWithRef(all, active, r)...)
		} else {
			speex = append(speex, c.ProcessWithRef(all[active], r)...)
		}
	}
	if *save != "" {
		b, err := c.ExportState()
		if err != nil {
			log.Fatal(err)
		}
		if err := os.WriteFile(*save, b, 0o644); err != nil {
			log.Fatal(err)
		}
	}
	for name, pcm := range map[string][]byte{"mic.wav": mic, "ref.wav": ref, "speex.wav": speex} {
		if err := writeWAV(filepath.Join(*out, name), pcm); err != nil {
			log.Fatal(err)
		}
	}
	log.Printf("%s: %.1fs -> %s", *in, float64(len(mic))/2/rate, *out)
}

// micPlan is which microphone is in use from each time onward.
type micPlan []struct {
	from float64
	ch   int
}

func parseMics(spec string) (micPlan, error) {
	if spec == "" {
		return nil, nil
	}
	var p micPlan
	for _, part := range strings.Split(spec, ",") {
		var from float64
		var ch int
		if _, err := fmt.Sscanf(part, "%g:%d", &from, &ch); err != nil || ch < 0 || ch >= beamformer.NumMics {
			return nil, fmt.Errorf("-mics: %q is not seconds:channel with a channel of 0-%d", part, beamformer.NumMics-1)
		}
		if len(p) > 0 && from <= p[len(p)-1].from {
			return nil, fmt.Errorf("-mics: times must increase, at %q", part)
		}
		p = append(p, struct {
			from float64
			ch   int
		}{from, ch})
	}
	return p, nil
}

func (p micPlan) at(t float64) int {
	ch := 6
	for _, e := range p {
		if t >= e.from {
			ch = e.ch
		}
	}
	return ch
}

func writeWAV(path string, pcm []byte) error {
	h := make([]byte, 44)
	copy(h, "RIFF")
	binary.LittleEndian.PutUint32(h[4:], uint32(36+len(pcm)))
	copy(h[8:], "WAVEfmt ")
	binary.LittleEndian.PutUint32(h[16:], 16)
	binary.LittleEndian.PutUint16(h[20:], 1)
	binary.LittleEndian.PutUint16(h[22:], 1)
	binary.LittleEndian.PutUint32(h[24:], rate)
	binary.LittleEndian.PutUint32(h[28:], rate*2)
	binary.LittleEndian.PutUint16(h[32:], 2)
	binary.LittleEndian.PutUint16(h[34:], 16)
	copy(h[36:], "data")
	binary.LittleEndian.PutUint32(h[40:], uint32(len(pcm)))
	return os.WriteFile(path, append(h, pcm...), 0o644)
}
