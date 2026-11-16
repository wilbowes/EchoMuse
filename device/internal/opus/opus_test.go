package opus

import (
	"encoding/binary"
	"math"
	"os"
	"testing"
)

// packets reads testdata/tones.pk: 1 s of stereo, 1 kHz left and 440 Hz right
// at -12 dBFS, encoded by libopus through PyAV as aiosendspin encodes.
func packets(t *testing.T) [][]byte {
	t.Helper()
	b, err := os.ReadFile("testdata/tones.pk")
	if err != nil {
		t.Fatal(err)
	}
	var out [][]byte
	for len(b) >= 2 {
		n := int(binary.BigEndian.Uint16(b))
		out = append(out, b[2:2+n])
		b = b[2+n:]
	}
	return out
}

func decodeAll(t *testing.T, channels int) []int16 {
	t.Helper()
	d, err := NewDecoder(channels)
	if err != nil {
		t.Fatal(err)
	}
	defer d.Close()
	var pcm []int16
	buf := make([]int16, MaxFrame*channels)
	for _, p := range packets(t) {
		n, err := d.Decode(p, buf)
		if err != nil {
			t.Fatal(err)
		}
		pcm = append(pcm, buf[:n*channels]...)
	}
	return pcm
}

// level is the dBFS of one frequency in channel ch of interleaved pcm, by
// Goertzel over the middle of the clip (past the encoder's start-up).
func level(pcm []int16, channels, ch int, hz float64) float64 {
	n := len(pcm) / channels
	from, to := n/4, n*3/4
	w := 2 * math.Pi * hz / Rate
	c := 2 * math.Cos(w)
	var s1, s2 float64
	for i := from; i < to; i++ {
		s := float64(pcm[i*channels+ch])/32768 + c*s1 - s2
		s2, s1 = s1, s
	}
	mag := math.Sqrt(s1*s1+s2*s2-c*s1*s2) / float64(to-from) * 2
	return 20 * math.Log10(mag)
}

func TestStereoKeepsEachSideItsOwnTone(t *testing.T) {
	pcm := decodeAll(t, 2)
	if got := len(pcm) / 2; got < Rate {
		t.Fatalf("decoded %d samples per channel, want at least %d", got, Rate)
	}
	for _, c := range []struct {
		ch         int
		own, other float64
	}{{0, 1000, 440}, {1, 440, 1000}} {
		own, other := level(pcm, 2, c.ch, c.own), level(pcm, 2, c.ch, c.other)
		if math.Abs(own-(-12)) > 1 {
			t.Errorf("channel %d: %g Hz at %.1f dBFS, want -12 ±1", c.ch, c.own, own)
		}
		if own-other < 40 {
			t.Errorf("channel %d: %g Hz only %.1f dB above %g Hz", c.ch, c.own, own-other, c.other)
		}
	}
}

// A stereo stream decoded to one channel is the average of the two sides, as
// the FLAC path downmixes: each tone 6 dB below where it was.
func TestStereoToMonoDownmixes(t *testing.T) {
	pcm := decodeAll(t, 1)
	for _, hz := range []float64{1000, 440} {
		if got := level(pcm, 1, 0, hz); math.Abs(got-(-18)) > 1 {
			t.Errorf("%g Hz at %.1f dBFS in the downmix, want -18 ±1", hz, got)
		}
	}
}

func TestPacketSamplesReadsTheTOC(t *testing.T) {
	for i, p := range packets(t) {
		n, err := PacketSamples(p)
		if err != nil || n != 960 {
			t.Fatalf("packet %d: %d samples, %v; want 960 (20 ms)", i, n, err)
		}
	}
	if _, err := PacketSamples(nil); err == nil {
		t.Error("an empty packet has no TOC, want an error")
	}
}

// An empty packet is a lost one: libopus conceals 20 ms rather than failing,
// and never more than that whatever room it is given.
func TestEmptyPacketConcealsOneFrame(t *testing.T) {
	d, _ := NewDecoder(2)
	defer d.Close()
	buf := make([]int16, MaxFrame*2)
	if _, err := d.Decode(packets(t)[0], buf); err != nil {
		t.Fatal(err)
	}
	n, err := d.Decode(nil, buf)
	if err != nil || n != 960 {
		t.Fatalf("concealment gave %d samples, %v; want 960", n, err)
	}
}

func TestRefusals(t *testing.T) {
	for _, ch := range []int{0, 3, -1} {
		if _, err := NewDecoder(ch); err == nil {
			t.Errorf("NewDecoder(%d) succeeded", ch)
		}
	}
	d, _ := NewDecoder(2)
	p := packets(t)[0]
	if _, err := d.Decode(p, make([]int16, 100)); err == nil {
		t.Error("decoded a 20 ms packet into 50 samples per channel")
	}
	if _, err := d.Decode(p, nil); err == nil {
		t.Error("decoded into nothing")
	}
	// A code-3 TOC (0xFF: CELT FB 20 ms, arbitrary frames) with no frame count.
	if _, err := d.Decode([]byte{0xFF}, make([]int16, MaxFrame*2)); err == nil {
		t.Error("decoded a truncated packet")
	}
	d.Close()
	d.Close()
	if _, err := d.Decode(p, make([]int16, MaxFrame*2)); err == nil {
		t.Error("decoded after Close")
	}
}

// After Reset the decoder gives the same output as a new one: nothing from the
// stream before the cut leaks into the one after it.
func TestResetForgetsTheStreamBefore(t *testing.T) {
	ps := packets(t)
	fresh, _ := NewDecoder(2)
	defer fresh.Close()
	want := make([]int16, MaxFrame*2)
	n, _ := fresh.Decode(ps[10], want)

	d, _ := NewDecoder(2)
	defer d.Close()
	buf := make([]int16, MaxFrame*2)
	for _, p := range ps[:10] {
		d.Decode(p, buf)
	}
	d.Reset()
	got := make([]int16, MaxFrame*2)
	if m, _ := d.Decode(ps[10], got); m != n {
		t.Fatalf("%d samples after Reset, %d fresh", m, n)
	}
	for i := 0; i < n*2; i++ {
		if got[i] != want[i] {
			t.Fatalf("sample %d is %d after Reset, %d from a new decoder", i, got[i], want[i])
		}
	}
}
