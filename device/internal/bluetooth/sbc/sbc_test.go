package sbc

import (
	"bytes"
	"testing"
)

// MaxBitpool from 16 per subband alone, 32 together, never past 250.
func TestMaxBitpool(t *testing.T) {
	for _, tc := range []struct {
		mode ChannelMode
		nsb  int
		want int
	}{
		{Mono, 4, 64}, {Mono, 8, 128}, {DualChannel, 4, 64}, {DualChannel, 8, 128},
		{Stereo, 4, 128}, {Stereo, 8, 250}, {JointStereo, 4, 128}, {JointStereo, 8, 250},
	} {
		if got := MaxBitpool(tc.mode, tc.nsb); got != tc.want {
			t.Errorf("%s/%d: %d, want %d", tc.mode, tc.nsb, got, tc.want)
		}
	}
}

func TestValidateEdges(t *testing.T) {
	ok := Config{44100, Mono, 16, 8, Loudness, 32}
	if err := ok.Validate(); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name string
		mod  func(*Config)
		ok   bool
	}{
		{"bitpool 2", func(c *Config) { c.Bitpool = 2 }, true},
		{"bitpool 1", func(c *Config) { c.Bitpool = 1 }, false},
		{"bitpool at mono limit", func(c *Config) { c.Bitpool = 128 }, true},
		{"bitpool past mono limit", func(c *Config) { c.Bitpool = 129 }, false},
		{"joint 250", func(c *Config) { c.Mode, c.Bitpool = JointStereo, 250 }, true},
		{"joint 251", func(c *Config) { c.Mode, c.Bitpool = JointStereo, 251 }, false},
		{"4 subbands mono 65", func(c *Config) { c.Subbands, c.Bitpool = 4, 65 }, false},
		{"22050 Hz", func(c *Config) { c.SampleRate = 22050 }, false},
		{"6 blocks", func(c *Config) { c.Blocks = 6 }, false},
		{"6 subbands", func(c *Config) { c.Subbands = 6 }, false},
		{"mode 4", func(c *Config) { c.Mode = 4 }, false},
		{"allocation 2", func(c *Config) { c.Allocation = 2 }, false},
	} {
		c := ok
		tc.mod(&c)
		if err := c.Validate(); (err == nil) != tc.ok {
			t.Errorf("%s: err=%v", tc.name, err)
		}
	}
}

// FrameLength against the spec's formula worked by hand; the libsbc streams check it
// again in ref_test.go.
func TestFrameLength(t *testing.T) {
	for _, tc := range []struct {
		c    Config
		want int
	}{
		{Config{44100, Mono, 16, 8, Loudness, 32}, 72},         // sbcinfo agrees
		{Config{44100, JointStereo, 16, 8, Loudness, 53}, 119}, // 4 + 8 + ceil((8+848)/8)
		{Config{44100, Stereo, 16, 8, Loudness, 53}, 118},
		{Config{16000, DualChannel, 4, 4, Loudness, 20}, 28},
		{Config{16000, Mono, 4, 4, Loudness, 2}, 7},
		{Config{16000, JointStereo, 4, 4, Loudness, 2}, 10}, // 4 + 4 + ceil(12/8)
	} {
		if got := tc.c.FrameLength(); got != tc.want {
			t.Errorf("%v: %d, want %d", tc.c, got, tc.want)
		}
	}
	if got := (Config{44100, JointStereo, 16, 8, Loudness, 53}).Bitrate(); got/1000 != 327 {
		t.Errorf("A2DP high quality: %d b/s, want ~328k", got)
	}
}

// The header's bit layout, and ParseHeader as its inverse for
// every legal combination.
func TestHeaderBits(t *testing.T) {
	b1, b2 := Config{44100, JointStereo, 16, 8, Loudness, 53}.header()
	if b1 != 0xBD || b2 != 53 {
		t.Errorf("44.1k joint 16/8 loudness: %02x %02x, want bd 35", b1, b2)
	}
	b1, _ = Config{16000, Mono, 4, 4, SNR, 2}.header()
	if b1 != 0x02 {
		t.Errorf("16k mono 4/4 snr: %02x, want 02", b1)
	}
	for _, c := range allConfigs(func(max int) []int { return []int{2, max} }) {
		h1, h2 := c.header()
		got, err := ParseHeader([]byte{syncword, h1, h2, 0})
		if err != nil || got != c {
			t.Fatalf("%v: parsed %v, %v", c, got, err)
		}
	}
	if _, err := ParseHeader([]byte{syncword, 0xBD}); err == nil {
		t.Error("short header accepted")
	}
	if _, err := ParseHeader([]byte{syncword, 0x00, 0x01, 0}); err == nil {
		t.Error("bitpool 1 accepted")
	}
}

// The CRC polynomial and start value: x^8+x^4+x^3+x^2+1 from 0x0F. No data
// leaves the start value; one 1 bit shifts it through the feedback.
func TestCRC(t *testing.T) {
	if got := crcUpdate(0x0F, nil, 0); got != 0x0F {
		t.Errorf("empty: %02x", got)
	}
	// 0x0F<<1 = 0x1E with feedback 0^1 = 1, so 0x1E ^ 0x1D.
	if got := crcUpdate(0x0F, []byte{0x80}, 1); got != 0x03 {
		t.Errorf("one bit: %02x, want 03", got)
	}
	// Bit-at-a-time over a partial byte equals the same bits as a whole
	// byte once the rest are zero and counted.
	a := crcUpdate(0x0F, []byte{0xA0}, 4)
	if b := crcUpdate(a, []byte{0x00}, 4); b != crcUpdate(0x0F, []byte{0xA0}, 8) {
		t.Error("partial-byte CRC does not compose")
	}
}

func TestBitWriterReader(t *testing.T) {
	buf := make([]byte, 8)
	w := bitWriter{buf: buf}
	fields := []struct {
		v uint32
		n uint
	}{{1, 1}, {0x5, 3}, {0xABCD, 16}, {0, 2}, {0x3FF, 10}, {1, 1}}
	for _, f := range fields {
		w.put(f.v, f.n)
	}
	w.flush()
	r := bitReader{buf: buf}
	for _, f := range fields {
		if got := r.get(f.n); got != f.v {
			t.Errorf("%d bits: %#x, want %#x", f.n, got, f.v)
		}
	}
	if !bytes.Equal(buf[:1], []byte{0xDA}) { // 1 101 1010
		t.Errorf("first byte %08b", buf[0])
	}
}

func BenchmarkEncodeJoint48(b *testing.B) {
	c := Config{48000, JointStereo, 16, 8, Loudness, 53}
	enc, _ := NewEncoder(c)
	pcm := signal("noise", c, 1, 1)
	dst := make([]byte, c.FrameLength())
	b.SetBytes(int64(len(pcm) * 2))
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		enc.Encode(dst, pcm)
	}
}

func BenchmarkEncodeJoint44(b *testing.B) {
	c := Config{44100, JointStereo, 16, 8, Loudness, 53}
	enc, _ := NewEncoder(c)
	pcm := signal("sine", c, 1, 1)
	dst := make([]byte, c.FrameLength())
	b.SetBytes(int64(len(pcm) * 2))
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		enc.Encode(dst, pcm)
	}
}
