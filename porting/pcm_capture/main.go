//go:build server

// pcm_capture: record any ALSA capture device to a raw interleaved file.
//
// Stock FireOS ships tinycap, but it maps "-b 24" to 4-byte S24_LE and has no
// packed 24-bit option, so it cannot open biscuit's mic array (S24_3LE) at
// any bit depth — measured on stock FireOS 5, 2026-09-18. This is
// device/tools/capture_mics with the card, device, channels, rate and format
// taken as flags instead of biscuit's constants.
//
// It does not stop anything. Whatever holds the device (mediaserver, on a
// stock Echo) must be stopped first, or the open blocks indefinitely — that
// is porting/probe.sh's job, which also restarts it.
//
// Build (from the repo root, needs the echomuse-compiler image):
//   porting/pcm_capture/build.sh
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/Binozo/GoTinyAlsa/pkg/pcm"
	"github.com/Binozo/GoTinyAlsa/pkg/tinyalsa"
)

func main() {
	card := flag.Int("D", 0, "ALSA card")
	device := flag.Int("d", 0, "ALSA device")
	channels := flag.Int("c", 2, "channels")
	rate := flag.Int("r", 16000, "sample rate")
	format := flag.String("f", "s16_le", "s16_le | s24_le | s24_3le | s32_le")
	seconds := flag.Int("t", 5, "seconds to record (1-60)")
	period := flag.Int("p", 256, "period size in frames")
	out := flag.String("o", "/data/local/tmp/pcm_capture.raw", "output file")
	flag.Parse()

	cfg := pcm.Config{
		Channels:    *channels,
		SampleRate:  *rate,
		PeriodSize:  *period,
		PeriodCount: 4,
	}
	// A switch rather than a map: pcm.Config's format type is internal to
	// GoTinyAlsa, so it can be assigned but not named here.
	bytesPer := 0
	switch *format {
	case "s16_le":
		cfg.Format, bytesPer = tinyalsa.PCM_FORMAT_S16_LE, 2
	case "s24_le":
		cfg.Format, bytesPer = tinyalsa.PCM_FORMAT_S24_LE, 4
	case "s24_3le":
		cfg.Format, bytesPer = tinyalsa.PCM_FORMAT_S24_3LE, 3
	case "s32_le":
		cfg.Format, bytesPer = tinyalsa.PCM_FORMAT_S32_LE, 4
	}
	if bytesPer == 0 || *seconds < 1 || *seconds > 60 || *channels < 1 {
		flag.Usage()
		os.Exit(2)
	}
	dev := tinyalsa.NewDevice(*card, *device, cfg)

	f, err := os.Create(*out)
	if err != nil {
		log.Fatalf("create %s: %v", *out, err)
	}
	defer f.Close()

	stream := make(chan []byte, 32)
	errCh := make(chan error, 1)
	go func() {
		if err := dev.GetAudioStream(dev.DeviceConfig, stream); err != nil {
			errCh <- err
		}
		close(errCh)
	}()

	deadline := time.After(time.Duration(*seconds) * time.Second)
	written := 0
loop:
	for {
		select {
		case <-deadline:
			break loop
		case err := <-errCh:
			if err != nil {
				log.Fatalf("ALSA stream error: %v", err)
			}
			break loop
		case buf, ok := <-stream:
			if !ok {
				break loop
			}
			n, err := f.Write(buf)
			if err != nil {
				log.Fatalf("write: %v", err)
			}
			written += n
		}
	}

	frames := written / (*channels * bytesPer)
	// One machine-readable line; probe.sh parses it.
	fmt.Printf("CAPTURED card=%d device=%d channels=%d rate=%d format=%s frames=%d bytes=%d file=%s\n",
		*card, *device, *channels, *rate, *format, frames, written, *out)
}
