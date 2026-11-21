package turnbeam

import (
	"encoding/binary"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// TestParityOnCaptures runs the stage over raw 4-mic captures and writes the
// cleaned beam of each turn for the offline scorer (Silero + Parakeet). Only
// with EM_PARITY="out_dir;cap_dir:t1,t2;cap_dir:t3..." (bench, 2026-11-21).
// Captures are r.raw (S32 × 4 at the capture's 24 dB extract headroom).
func TestParityOnCaptures(t *testing.T) {
	spec := os.Getenv("EM_PARITY")
	if spec == "" {
		t.Skip("EM_PARITY not set")
	}
	parts := strings.Split(spec, ";")
	out := parts[0]
	for _, p := range parts[1:] {
		dt := strings.SplitN(p, ":", 2)
		dir := dt[0]
		b, err := os.ReadFile(filepath.Join(dir, "r.raw"))
		if err != nil {
			t.Fatal(err)
		}
		n := len(b) / 16
		mics := make([][]int16, 4)
		scale := math.Pow(10, 24.0/20) / 65536
		for c := range mics {
			mics[c] = make([]int16, n)
			for i := 0; i < n; i++ {
				v := float64(int32(binary.LittleEndian.Uint32(b[16*i+4*c:]))) * scale
				mics[c][i] = clip16(v)
			}
		}
		for _, ts := range strings.Split(dt[1], ",") {
			tc, _ := strconv.ParseFloat(ts, 64)
			s := New(Config{Mics: 4})
			batch := 2560
			cross := int(tc * sampleRate)
			lockAt := (cross/batch + 1) * batch
			end := cross + 4*sampleRate
			var y []int16
			start := 0.0
			for i := 0; i+batch <= n && i < end; i += batch {
				if i == lockAt {
					o, t0, err := s.Lock(float64(lockAt-cross) / sampleRate)
					start = t0
					if err != nil {
						t.Fatalf("%s %.2f: %v", dir, tc, err)
					}
					y = append(y, o...)
				}
				ch := make([][]int16, 4)
				for c := range ch {
					ch[c] = mics[c][i : i+batch]
				}
				o := s.Push(ch)
				if s.Locked() {
					y = append(y, o...)
				}
			}
			name := fmt.Sprintf("%s_%.2f", filepath.Base(dir), tc)
			raw := make([]byte, 2*len(y))
			for i, v := range y {
				binary.LittleEndian.PutUint16(raw[2*i:], uint16(v))
			}
			if err := os.WriteFile(filepath.Join(out, name+".pcm"), raw, 0o644); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(out, name+".floor"), []byte(fmt.Sprintf("%.3f %.6f\n", s.FloorDB(), start)), 0o644); err != nil {
				t.Fatal(err)
			}
			t.Logf("%s: %d samples from %.4fs around the crossing, floor %.1f dB", name, len(y), start, s.FloorDB())
		}
	}
}

// TestProbeParityOnCaptures takes a second look at each given near-miss
// ~1.2s after it (as the device does) and writes the probe's beam and its
// start time. Only with EM_PROBE (same format as EM_PARITY).
func TestProbeParityOnCaptures(t *testing.T) {
	spec := os.Getenv("EM_PROBE")
	if spec == "" {
		t.Skip("EM_PROBE not set")
	}
	parts := strings.Split(spec, ";")
	out := parts[0]
	for _, p := range parts[1:] {
		dt := strings.SplitN(p, ":", 2)
		dir := dt[0]
		b, err := os.ReadFile(filepath.Join(dir, "r.raw"))
		if err != nil {
			t.Fatal(err)
		}
		n := len(b) / 16
		mics := make([][]int16, 4)
		scale := math.Pow(10, 24.0/20) / 65536
		for c := range mics {
			mics[c] = make([]int16, n)
			for i := 0; i < n; i++ {
				mics[c][i] = clip16(float64(int32(binary.LittleEndian.Uint32(b[16*i+4*c:]))) * scale)
			}
		}
		for _, ts := range strings.Split(dt[1], ",") {
			tc, _ := strconv.ParseFloat(ts, 64)
			we, _ := strconv.ParseFloat(os.Getenv("EM_WAKE_END"), 64)
			s := New(Config{Mics: 4, WakeEndS: we})
			batch := 2560
			cross := int(tc * sampleRate)
			at := ((cross+int(1.2*sampleRate))/batch + 1) * batch
			for i := 0; i+batch <= at && i+batch <= n; i += batch {
				ch := make([][]int16, 4)
				for c := range ch {
					ch[c] = mics[c][i : i+batch]
				}
				s.Push(ch)
			}
			pr, err := s.Probe(float64(at-cross) / sampleRate)
			if err != nil {
				t.Fatalf("%s %.2f: %v", dir, tc, err)
			}
			y := pr.Beam()
			t.Logf("%s %.2f: lead %.3fs, ref ch%d, start %.4f", filepath.Base(dir), tc, float64(at-cross)/sampleRate, pr.b.ref, pr.Start())
			raw := make([]byte, 2*len(y))
			for i, v := range y {
				binary.LittleEndian.PutUint16(raw[2*i:], uint16(v))
			}
			name := fmt.Sprintf("probe_%s_%.2f", filepath.Base(dir), tc)
			os.WriteFile(filepath.Join(out, name+".pcm"), raw, 0o644)
			os.WriteFile(filepath.Join(out, name+".start"), []byte(fmt.Sprintf("%.6f\n", pr.Start())), 0o644)
		}
	}
}
