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
// There is no playback-level option: since #638 the volume is applied in
// software before the loopback, so the reference already carries it. A
// capture from firmware before that has a pre-volume reference and does not
// replay faithfully here.
package main

import (
	"encoding/binary"
	"flag"
	"log"
	"math"
	"os"
	"path/filepath"

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
	tail := flag.Int("tail", 300, "speex filter tail, ms")
	gainDb := flag.Float64("gain", 24, "mic gain, dB (micGainDb)")
	load := flag.String("load", "", "start from a saved echo path (aec.ExportState) instead of cold")
	save := flag.String("save", "", "write the echo path learnt by the end of this run")
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
	for off := 0; off+step <= len(raw); off += step {
		period := raw[off : off+step]
		m, _ := bf.Process(period, -1, gain) // unlocked: ch6, as the wake stream
		r := bf.EchoRef(period)
		mic = append(mic, m...)
		ref = append(ref, r...)
		speex = append(speex, c.ProcessWithRef(m, r)...)
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
