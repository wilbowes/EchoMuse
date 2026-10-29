package sbc

import (
	"bufio"
	"encoding/binary"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Fixtures from libsbc's own sbcenc/sbcdec; testdata/gen.sh makes them.

func refCases(t *testing.T) []string {
	t.Helper()
	f, err := os.Open("testdata/cases.txt")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var names []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || line[0] == '#' {
			continue
		}
		names = append(names, strings.Fields(line)[0])
	}
	return names
}

// readAU reads a 16-bit linear Sun .au file as interleaved samples.
func readAU(t *testing.T, path string) ([]int16, int) {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(b) < 24 || string(b[:4]) != ".snd" || binary.BigEndian.Uint32(b[12:]) != 3 {
		t.Fatalf("%s: not 16-bit linear .au", path)
	}
	off := int(binary.BigEndian.Uint32(b[4:]))
	ch := int(binary.BigEndian.Uint32(b[20:]))
	data := b[off:]
	out := make([]int16, len(data)/2)
	for i := range out {
		out[i] = int16(binary.BigEndian.Uint16(data[2*i:]))
	}
	return out, ch
}

func readFile(t *testing.T, path string) []byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// decodeAll decodes a stream of frames with our decoder.
func decodeAll(t *testing.T, stream []byte) ([]int16, Config) {
	t.Helper()
	dec := NewDecoder()
	var out []int16
	var cfg Config
	buf := make([]int16, 16*8*2)
	for off := 0; off < len(stream); {
		c, n, err := dec.Decode(stream[off:], buf)
		if err != nil {
			t.Fatalf("decode at %d: %v", off, err)
		}
		cfg = c
		out = append(out, buf[:c.SamplesPerFrame()*c.Channels()]...)
		off += n
	}
	return out, cfg
}

// Every libsbc frame: the header parses, our CRC equals libsbc's, and frames
// sit exactly FrameLength apart.
func TestReferenceFramesHeaderCRCAndLength(t *testing.T) {
	for _, name := range refCases(t) {
		stream := readFile(t, "testdata/ref_"+name+".sbc")
		frames := 0
		for off := 0; off < len(stream); frames++ {
			c, err := ParseHeader(stream[off:])
			if err != nil {
				t.Fatalf("%s frame %d: %v", name, frames, err)
			}
			if got := frameCRC(stream[off:], c.sideBits()); got != stream[off+3] {
				t.Fatalf("%s frame %d: crc %02x, libsbc wrote %02x", name, frames, got, stream[off+3])
			}
			off += c.FrameLength()
			if off > len(stream) {
				t.Fatalf("%s: FrameLength %d overruns the stream", name, c.FrameLength())
			}
		}
		if frames == 0 {
			t.Fatalf("%s: no frames", name)
		}
	}
}

// Our decoder on libsbc's frames reproduces libsbc's own decode.
func TestDecoderMatchesReferenceDecoder(t *testing.T) {
	for _, name := range refCases(t) {
		got, cfg := decodeAll(t, readFile(t, "testdata/ref_"+name+".sbc"))
		want, ch := readAU(t, "testdata/refdec_"+name+".au")
		if ch != cfg.Channels() || len(got) != len(want) {
			t.Fatalf("%s: %d samples x%d, reference %d x%d", name, len(got), cfg.Channels(), len(want), ch)
		}
		s, gain, delay := snr(want, got, ch, 0)
		t.Logf("%s (%v): %.1f dB against sbcdec, gain %.4f, delay %d", name, cfg, s, gain, delay)
		if delay != 0 || s < 60 {
			t.Errorf("%s: %.1f dB at delay %d against libsbc's decode, want >= 60 dB at 0", name, s, delay)
		}
	}
}

// Our encoder's frames, decoded by libsbc's sbcdec when the fixtures were
// made, reproduce the input; and our encoder still produces those frames.
func TestReferenceDecoderAcceptsOurFrames(t *testing.T) {
	for _, name := range refCases(t) {
		in, ch := readAU(t, "testdata/in_"+name+".au")
		ref, _ := readAU(t, "testdata/oursdec_"+name+".au")
		cfg, err := ParseHeader(readFile(t, "testdata/ref_"+name+".sbc"))
		if err != nil {
			t.Fatal(err)
		}
		// libsbc encoding the same input is the quality bar.
		libsbc, _ := readAU(t, "testdata/refdec_"+name+".au")
		bar, _, _ := snr(in, libsbc, ch, 200)
		s, gain, delay := snr(in, ref, ch, 200)
		t.Logf("%s: sbcdec of our frames %.1f dB (libsbc's own %.1f), gain %.4f, delay %d",
			name, s, bar, gain, delay)
		if s < bar-1 || gain < 0.99 || gain > 1.01 || delay != cfg.Subbands*9+1 {
			t.Errorf("%s: sbcdec of our frames %.1f dB gain %.3f delay %d, want >= %.1f dB at unity, delay %d",
				name, s, gain, delay, bar-1, cfg.Subbands*9+1)
		}
		// Our current output against the frames sbcdec judged. Not byte
		// equality: a compiler may fuse float multiply-adds differently.
		_, stream := roundTrip(t, cfg, in)
		cur, _ := decodeAll(t, stream)
		old, _ := decodeAll(t, readFile(t, "testdata/ours_"+name+".sbc"))
		if s, _, d := snr(old, cur, ch, 0); d != 0 || s < 50 {
			t.Errorf("%s: the encoder has changed since gen.sh (%.1f dB at delay %d); rerun testdata/gen.sh", name, s, d)
		}
	}
}

// TestDumpOurs writes our frames for gen.sh to hand to sbcdec.
func TestDumpOurs(t *testing.T) {
	if os.Getenv("SBC_DUMP") == "" {
		t.Skip("testdata/gen.sh sets SBC_DUMP")
	}
	for _, name := range refCases(t) {
		in, _ := readAU(t, "testdata/in_"+name+".au")
		cfg, err := ParseHeader(readFile(t, "testdata/ref_"+name+".sbc"))
		if err != nil {
			t.Fatal(err)
		}
		_, stream := roundTrip(t, cfg, in)
		if err := os.WriteFile(filepath.Join("testdata", "ours_"+name+".sbc"), stream, 0o644); err != nil {
			t.Fatal(err)
		}
	}
}
