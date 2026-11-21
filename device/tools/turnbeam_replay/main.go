// turnbeam_replay runs a bench raw capture (rawtap_bench.go: r.raw, the
// board's own frame) through internal/turnbeam on the host, locking at the
// given wake-crossing times, and writes per lock:
//
//	lock<N>_beam.wav       the beam from the lock for 15s
//	lock<N>_endpoint.wav   the beam from the lock to the stage's endpoint
//	lock<N>_mic.wav        the stage's reference mic over the same 15s
//
// It prints the endpoint, the threshold, and how far the beam took the
// audio 3-12s after the lock (the TV tail in the lounge captures) below the
// reference mic. Usage (compiler image, GOOS=linux GOARCH=amd64):
//
//	go run ./tools/turnbeam_replay -in cap/w7 -ch 4 -lock 17.18,40.85,83.75 -out out/w7
//
// -gain is the dB applied to the 32-bit samples before S16, as the device's
// extraction does with micGainDb (24 by default; the AEC headroom is given
// back on a turn).
package main

import (
	"encoding/binary"
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/wilbowes/EchoMuse/internal/turnbeam"
)

const rate = 16000

func main() {
	in := flag.String("in", "", "capture directory holding r.raw")
	ch := flag.Int("ch", 4, "channels in r.raw (S32LE)")
	gain := flag.Float64("gain", 24, "dB on the 32-bit samples before S16")
	locks := flag.String("lock", "", "wake crossing times, seconds from the capture start, comma-separated")
	out := flag.String("out", "out", "output directory")
	flag.Parse()

	raw, err := os.ReadFile(filepath.Join(*in, "r.raw"))
	if err != nil {
		log.Fatal(err)
	}
	n := len(raw) / (4 * *ch)
	g := math.Pow(10, *gain/20) / 65536
	mics := make([][]int16, *ch)
	for c := range mics {
		mics[c] = make([]int16, n)
	}
	for i := 0; i < n; i++ {
		for c := 0; c < *ch; c++ {
			v := float64(int32(binary.LittleEndian.Uint32(raw[(i**ch+c)*4:]))) * g
			mics[c][i] = clip(v)
		}
	}
	if err := os.MkdirAll(*out, 0o755); err != nil {
		log.Fatal(err)
	}
	for li, ls := range strings.Split(*locks, ",") {
		t, err := strconv.ParseFloat(strings.TrimSpace(ls), 64)
		if err != nil {
			log.Fatal(err)
		}
		lockAt := int(t*rate) / 2560 * 2560
		start := lockAt - 9*rate
		if start < 0 {
			start = 0
		}
		start = lockAt - (lockAt-start)/2560*2560
		st := turnbeam.New(turnbeam.Config{Mics: *ch})
		var beam, ref []int16
		endS := -1.0
		for i := start; i+2560 <= n && i < lockAt+15*rate; i += 2560 {
			if i == lockAt {
				if err := st.Lock(); err != nil {
					log.Fatalf("lock %d at %.2fs: %v", li, t, err)
				}
			}
			chunk := make([][]int16, *ch)
			for c := range chunk {
				chunk[c] = mics[c][i : i+2560]
			}
			y := st.Push(chunk)
			if st.Locked() {
				beam = append(beam, y...)
				ref = append(ref, chunk[st.RefMic()][:len(y)]...) // (the beam is GateDelay+hop behind; dB windows are seconds long)
				if st.Ended() && endS < 0 {
					endS = float64(i+2560-lockAt) / rate
				}
			}
		}
		endN := len(beam)
		if endS >= 0 {
			endN = int(endS * rate)
		}
		p := func(name string, x []int16) {
			if err := writeWav(filepath.Join(*out, fmt.Sprintf("lock%d_%s.wav", li, name)), x); err != nil {
				log.Fatal(err)
			}
		}
		p("beam", beam)
		p("mic", ref)
		p("endpoint", beam[:endN])
		drop := db(ref, 3*rate, 12*rate) - db(beam, 3*rate, 12*rate)
		fmt.Printf("lock %d at %.2fs: ref ch%d, threshold %.2f, wake score %.2f, endpoint %s, 3-12s after the lock the beam is %.1f dB under the mic\n",
			li, t, st.RefMic(), st.Threshold(), st.WakeScore(), fmtEnd(endS), drop)
		var tr []string
		for _, v := range st.Trace {
			tr = append(tr, fmt.Sprintf("%.1f", v))
		}
		fmt.Printf("  trace (128ms blocks from the lock): %s\n", strings.Join(tr, " "))
	}
}

func fmtEnd(s float64) string {
	if s < 0 {
		return "none in 15s"
	}
	return fmt.Sprintf("%.2fs after the lock", s)
}

func db(x []int16, a, b int) float64 {
	if b > len(x) {
		b = len(x)
	}
	s := 0.0
	for _, v := range x[a:b] {
		s += float64(v) * float64(v)
	}
	return 10 * math.Log10(s/float64(b-a)+1e-9)
}

func clip(v float64) int16 {
	if v > 32767 {
		return 32767
	}
	if v < -32768 {
		return -32768
	}
	return int16(math.Round(v))
}

func writeWav(path string, x []int16) error {
	b := make([]byte, 44+2*len(x))
	copy(b, "RIFF")
	binary.LittleEndian.PutUint32(b[4:], uint32(36+2*len(x)))
	copy(b[8:], "WAVEfmt ")
	binary.LittleEndian.PutUint32(b[16:], 16)
	binary.LittleEndian.PutUint16(b[20:], 1)
	binary.LittleEndian.PutUint16(b[22:], 1)
	binary.LittleEndian.PutUint32(b[24:], rate)
	binary.LittleEndian.PutUint32(b[28:], rate*2)
	binary.LittleEndian.PutUint16(b[32:], 2)
	binary.LittleEndian.PutUint16(b[34:], 16)
	copy(b[36:], "data")
	binary.LittleEndian.PutUint32(b[40:], uint32(2*len(x)))
	for i, v := range x {
		binary.LittleEndian.PutUint16(b[44+2*i:], uint16(v))
	}
	return os.WriteFile(path, b, 0o644)
}
