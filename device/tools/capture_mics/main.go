//go:build server

// capture_mics: captures raw 9-channel audio from the biscuit mic array
// and writes it to /data/local/tmp/capture.raw for offline analysis.
//
// Usage:
//   capture_mics [seconds]   default: 5
//
// Output format: raw interleaved S24_3LE, 9 channels, 16kHz
// Each frame: 9 samples × 3 bytes = 27 bytes
// Each period (512 frames): 13,824 bytes
//
// Build inside echomuse-compiler Docker container:
//   go build -tags server -o capture_mics .

package main

import (
	"fmt"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/Binozo/GoTinyAlsa/pkg/pcm"
	"github.com/Binozo/GoTinyAlsa/pkg/tinyalsa"
)

const (
	cardNr    = 0
	deviceNr  = 24
	channels  = 9
	sampleRate = 16000
	periodSize = 512
	periodCount = 5
	outputPath = "/data/local/tmp/capture.raw"
)

func main() {
	durationSecs := 5
	if len(os.Args) > 1 {
		n, err := strconv.Atoi(os.Args[1])
		if err != nil || n < 1 || n > 60 {
			log.Fatalf("usage: capture_mics [seconds 1-60]")
		}
		durationSecs = n
	}

	// Must stop the mixer service to release the ALSA capture device —
	// same requirement as EchoMuse pcm_microphone.go
	fmt.Println("Stopping mixer service...")
	// Use exec if available, fall back silently
	stopMixer()

	fmt.Printf("Opening ALSA card %d device %d: %d channels, %dHz, S24_3LE\n",
		cardNr, deviceNr, channels, sampleRate)

	device := tinyalsa.NewDevice(cardNr, deviceNr, pcm.Config{
		Channels:    channels,
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

	framesWritten := bytesWritten / (channels * 3) // 3 bytes per S24_3LE sample
	durationMs := framesWritten * 1000 / sampleRate

	fmt.Printf("Done.\n")
	fmt.Printf("  Periods:  %d\n", periodsWritten)
	fmt.Printf("  Frames:   %d\n", framesWritten)
	fmt.Printf("  Duration: %dms\n", durationMs)
	fmt.Printf("  Bytes:    %d\n", bytesWritten)
	fmt.Printf("  File:     %s\n", outputPath)
	fmt.Printf("\nPull with: adb pull %s\n", outputPath)
}
