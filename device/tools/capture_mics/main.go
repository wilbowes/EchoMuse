//go:build server

// capture_mics: captures raw mic array audio from a device's ALSA card and
// writes it to /data/local/tmp/capture.raw for offline analysis.
//
// Usage:
//   capture_mics [seconds] [-card N] [-device N] [-channels N]
//   defaults: 5s, card 0, device 24, 9 channels — the biscuit (Echo Dot) mic
//
// For crown (Echo Show 8): -device 22 -channels 6
//   (card0,device22, 2x TLV320AIC3101, per docs/echo-show-8-hardware-map.md)
//
// Output format: raw interleaved S24_3LE at 16kHz, N channels
// Each frame: N samples × 3 bytes
// Each period (512 frames): N * 512 * 3 bytes
//
// Build inside echomuse-compiler Docker container:
//   go build -tags server -o capture_mics .

package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/Binozo/GoTinyAlsa/pkg/pcm"
	"github.com/Binozo/GoTinyAlsa/pkg/tinyalsa"
)

const (
	sampleRate  = 16000
	periodSize  = 512
	periodCount = 5
	outputPath  = "/data/local/tmp/capture.raw"
)

func main() {
	cardNr := flag.Int("card", 0, "ALSA card number")
	deviceNr := flag.Int("device", 24, "ALSA device number (24=biscuit mic, 22=crown mic)")
	channels := flag.Int("channels", 9, "channel count (9=biscuit, 6=crown)")
	flag.Parse()

	durationSecs := 5
	if flag.NArg() > 0 {
		n, err := strconv.Atoi(flag.Arg(0))
		if err != nil || n < 1 || n > 60 {
			log.Fatalf("usage: capture_mics [seconds 1-60] [-card N] [-device N] [-channels N]")
		}
		durationSecs = n
	}

	// Must stop the mixer service to release the ALSA capture device —
	// same requirement as EchoMuse pcm_microphone.go
	fmt.Println("Stopping mixer service...")
	// Use exec if available, fall back silently
	stopMixer()

	fmt.Printf("Opening ALSA card %d device %d: %d channels, %dHz, S24_3LE\n",
		*cardNr, *deviceNr, *channels, sampleRate)

	device := tinyalsa.NewDevice(*cardNr, *deviceNr, pcm.Config{
		Channels:    *channels,
		SampleRate:  sampleRate,
		PeriodSize:  periodSize,
		PeriodCount: periodCount,
		Format:      tinyalsa.PCM_FORMAT_S24_3LE,
	})

	// Open output file
	f, err := os.Create(outputPath)
	if err != nil {
		log.Fatalf("failed to create output file %s: %v", outputPath, err)
	}
	defer f.Close()

	// Start stream
	stream := make(chan []byte, 32)
	errCh := make(chan error, 1)
	go func() {
		if err := device.GetAudioStream(device.DeviceConfig, stream); err != nil {
			errCh <- err
		}
		close(errCh)
	}()

	deadline := time.After(time.Duration(durationSecs) * time.Second)
	bytesWritten := 0
	periodsWritten := 0

	fmt.Printf("Capturing %d seconds to %s ...\n", durationSecs, outputPath)

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
				log.Fatalf("write error: %v", err)
			}
			bytesWritten += n
			periodsWritten++
		}
	}

	framesWritten := bytesWritten / (*channels * 3) // 3 bytes per S24_3LE sample
	durationMs := framesWritten * 1000 / sampleRate

	fmt.Printf("Done.\n")
	fmt.Printf("  Periods:  %d\n", periodsWritten)
	fmt.Printf("  Frames:   %d\n", framesWritten)
	fmt.Printf("  Duration: %dms\n", durationMs)
	fmt.Printf("  Bytes:    %d\n", bytesWritten)
	fmt.Printf("  File:     %s\n", outputPath)
	fmt.Printf("\nPull with: adb pull %s\n", outputPath)
}
