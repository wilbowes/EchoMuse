// sendspin_bench answers one question: can this device afford to be a
// Sendspin player at all?
//
// Sendspin (#89) would put a client on the device talking straight to Music
// Assistant, which means the device pays, per second of audio, for:
//
//	1. ChaCha20-Poly1305 decryption   — Noise is MANDATORY in the spec, and
//	   the whole WebSocket payload rides inside it.
//	2. Audio decode                   — FLAC if we advertise it, nothing at
//	   all if we advertise PCM and eat the bandwidth instead.
//	3. A 2-D Kalman time filter       — runs once per sync message (~1/s) on
//	   a handful of scalars. Not measured: it is noise next to the above.
//
// The budget it has to fit inside is already committed: ~18-20% of a core on
// the mic pipeline, ~38% on shadow wake word. So this prints cost as **% of
// one core**, which is the number that decides the design, and it prints it
// for both codec choices so the trade is visible rather than argued.
//
// Cost is measured as WALL time with GOMAXPROCS=1, deliberately. CPU time
// would flatter the result by hiding scheduler contention, and contention is
// exactly what this device has — the answer we need is "what does it cost
// here, alongside everything else already running", not "what would it cost
// on an idle core".
//
// Build:  device/tools/build_tools.sh  (standalone module, like capture_mics)
// Run:    sendspin_bench -flac <file.flac> [-seconds 20]
//
// It opens no PCM, touches no mixer control and stops no service. Safe to run
// against a live device — see the shell-plane trap in the Sendspin notes.
package main

import (
	"bytes"
	"crypto/rand"
	"flag"
	"fmt"
	"io"
	"os"
	"runtime"
	"time"

	"github.com/mewkiz/flac"
	"golang.org/x/crypto/chacha20poly1305"
	"golang.org/x/crypto/curve25519"
)

// The stream Sendspin would actually carry. MA supports 16-bit only, and the
// device's speaker path is already 48kHz.
const (
	sampleRate = 48000
	channels   = 2
	bytesPerSample = 2
	pcmBytesPerSec = sampleRate * channels * bytesPerSample // 192000 B/s = 1.536 Mbps

	// WebSocket payload chunk. Sendspin does not fix this; 20ms is the
	// common choice and small chunks are the pessimistic case for the AEAD
	// (per-chunk tag + setup dominates at small sizes).
	chunkMS = 20
)

func main() {
	flacPath := flag.String("flac", "", "path to a FLAC file to decode (required)")
	seconds := flag.Float64("seconds", 20, "target seconds of audio to measure per test")
	flag.Parse()

	// Pin to one core so the percentages mean "% of one core" without
	// arithmetic. The real client would be one goroutine on one core too.
	runtime.GOMAXPROCS(1)

	fmt.Printf("sendspin_bench — %s/%s, %d CPU(s) visible\n\n",
		runtime.GOOS, runtime.GOARCH, runtime.NumCPU())

	if *flacPath == "" {
		fmt.Fprintln(os.Stderr, "need -flac <file>")
		os.Exit(2)
	}
	raw, err := os.ReadFile(*flacPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "read %s: %v\n", *flacPath, err)
		os.Exit(1)
	}

	flacSecs, fileSecs, flacCPU := benchFLAC(raw, *seconds)
	if flacSecs == 0 {
		fmt.Fprintln(os.Stderr, "decoded no audio — is that a valid FLAC?")
		os.Exit(1)
	}
	// Compressed bitrate of the actual file, so the AEAD figure below is
	// measured against the byte volume FLAC would really put on the wire.
	// Divided by the FILE's duration, not the total decoded across repeated
	// passes — the file is re-read each pass and its size does not grow.
	flacBytesPerSec := float64(len(raw)) / fileSecs

	aeadFLAC := benchAEAD(flacBytesPerSec, *seconds)
	aeadPCM := benchAEAD(pcmBytesPerSec, *seconds)
	dh := benchX25519()

	fmt.Printf("input            %.1fs of audio, %.0f kbps compressed (%.0f%% of PCM)\n\n",
		flacSecs, flacBytesPerSec*8/1000, flacBytesPerSec/pcmBytesPerSec*100)

	fmt.Println("per second of audio, as % of ONE core:")
	fmt.Printf("  FLAC decode                    %6.2f %%\n", flacCPU*100)
	fmt.Printf("  ChaCha20-Poly1305 @ FLAC rate  %6.3f %%\n", aeadFLAC*100)
	fmt.Printf("  ChaCha20-Poly1305 @ PCM rate   %6.3f %%\n", aeadPCM*100)
	fmt.Println()
	fmt.Printf("  TOTAL, advertise flac          %6.2f %%\n", (flacCPU+aeadFLAC)*100)
	fmt.Printf("  TOTAL, advertise pcm           %6.2f %%   (+%.0f kbps on the wire)\n",
		aeadPCM*100, (pcmBytesPerSec-flacBytesPerSec)*8/1000)
	fmt.Println()
	fmt.Printf("one-off, per connection:\n")
	fmt.Printf("  X25519 scalar mult             %6.2f ms   → Noise KKpsk2 ~%.1f ms (3 DH)\n",
		dh*1000, dh*3*1000)
	fmt.Println()
	fmt.Println("against a committed budget of ~18-20% (mic) + ~38% (shadow wake word).")
}

// benchFLAC decodes the file end to end, repeatedly, until it has covered at
// least `target` seconds of audio. Returns (audio seconds decoded, the file's
// own duration, cost as a fraction of one core: 1.0 means realtime decode
// saturates a core).
func benchFLAC(raw []byte, target float64) (float64, float64, float64) {
	var audioSecs, elapsed, fileSecs float64
	for audioSecs < target {
		s, d, err := decodeOnce(raw)
		if err != nil {
			fmt.Fprintf(os.Stderr, "flac decode: %v\n", err)
			return 0, 0, 0
		}
		if s == 0 {
			return 0, 0, 0
		}
		fileSecs = s
		audioSecs += s
		elapsed += d
	}
	return audioSecs, fileSecs, elapsed / audioSecs
}

// decodeOnce returns (seconds of audio, seconds of wall time). Parsing is
// inside the timed region on purpose: a real client parses frames off the
// wire as they arrive and pays for it every time.
func decodeOnce(raw []byte) (float64, float64, error) {
	start := time.Now()
	stream, err := flac.Parse(bytes.NewReader(raw))
	if err != nil {
		return 0, 0, err
	}
	var frames uint64
	rate := uint64(sampleRate)
	if stream.Info != nil && stream.Info.SampleRate > 0 {
		rate = uint64(stream.Info.SampleRate)
	}
	for {
		frame, err := stream.ParseNext()
		if err == io.EOF {
			break
		}
		if err != nil {
			return 0, 0, err
		}
		frames += uint64(frame.BlockSize)
		// Touch the samples. Without this the compiler is free to elide
		// work the real client would do when it copies into the mixer.
		for _, sub := range frame.Subframes {
			if len(sub.Samples) > 0 {
				_ = sub.Samples[len(sub.Samples)-1]
			}
		}
	}
	return float64(frames) / float64(rate), time.Since(start).Seconds(), nil
}

// benchAEAD measures ChaCha20-Poly1305 Open over `target` seconds' worth of a
// stream running at bytesPerSec, chunked as the WebSocket would chunk it.
// Returns cost as a fraction of one core.
func benchAEAD(bytesPerSec, target float64) float64 {
	key := make([]byte, chacha20poly1305.KeySize)
	rand.Read(key)
	aead, err := chacha20poly1305.New(key)
	if err != nil {
		fmt.Fprintf(os.Stderr, "chacha: %v\n", err)
		return 0
	}

	chunk := int(bytesPerSec * chunkMS / 1000)
	if chunk < 1 {
		chunk = 1
	}
	plain := make([]byte, chunk)
	rand.Read(plain)
	nonce := make([]byte, chacha20poly1305.NonceSize)
	sealed := aead.Seal(nil, nonce, plain, nil)

	// Decrypt, not encrypt: the device is the receiver. Open also verifies
	// the tag, which encryption-only timing would leave out.
	n := int(target * 1000 / chunkMS)
	out := make([]byte, 0, chunk)
	start := time.Now()
	for i := 0; i < n; i++ {
		out, err = aead.Open(out[:0], nonce, sealed, nil)
		if err != nil {
			fmt.Fprintf(os.Stderr, "chacha open: %v\n", err)
			return 0
		}
	}
	elapsed := time.Since(start).Seconds()
	audioSecs := float64(n) * chunkMS / 1000
	return elapsed / audioSecs
}

// benchX25519 returns seconds per scalar multiplication. KKpsk2 does three of
// them; the rest of the handshake is hashing and is not worth separating.
func benchX25519() float64 {
	var priv, peer [32]byte
	rand.Read(priv[:])
	rand.Read(peer[:])
	pub, err := curve25519.X25519(priv[:], curve25519.Basepoint)
	if err != nil {
		return 0
	}
	const n = 200
	start := time.Now()
	for i := 0; i < n; i++ {
		if _, err := curve25519.X25519(priv[:], pub); err != nil {
			return 0
		}
	}
	return time.Since(start).Seconds() / n
}
