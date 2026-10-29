package sbc

import (
	"math"
	"math/rand"
	"testing"
)

// signal makes n frames' worth of interleaved PCM: kind "sine" (a different
// tone per channel), "noise" or "silence".
func signal(kind string, c Config, frames int, seed int64) []int16 {
	ch := c.Channels()
	n := c.SamplesPerFrame() * frames
	out := make([]int16, n*ch)
	rng := rand.New(rand.NewSource(seed))
	for t := 0; t < n; t++ {
		for i := 0; i < ch; i++ {
			var v float64
			switch kind {
			case "sine":
				f := 440.0 * float64(i+1) * float64(c.SampleRate) / 44100
				if f > float64(c.SampleRate)/4 {
					f = float64(c.SampleRate) / 8
				}
				v = 12000 * math.Sin(2*math.Pi*f*float64(t)/float64(c.SampleRate))
			case "noise":
				v = rng.NormFloat64() * 4000
			}
			out[t*ch+i] = int16(v)
		}
	}
	return out
}

// roundTrip encodes pcm and decodes the result.
func roundTrip(t *testing.T, c Config, pcm []int16) ([]int16, []byte) {
	t.Helper()
	enc, err := NewEncoder(c)
	if err != nil {
		t.Fatalf("%v: %v", c, err)
	}
	per := c.SamplesPerFrame() * c.Channels()
	fl := c.FrameLength()
	stream := make([]byte, 0, fl*len(pcm)/per)
	frame := make([]byte, fl)
	for off := 0; off+per <= len(pcm); off += per {
		n, err := enc.Encode(frame, pcm[off:off+per])
		if err != nil || n != fl {
			t.Fatalf("%v: encode n=%d err=%v", c, n, err)
		}
		stream = append(stream, frame...)
	}
	dec := NewDecoder()
	out := make([]int16, 0, len(pcm))
	buf := make([]int16, per)
	for off := 0; off < len(stream); {
		got, n, err := dec.Decode(stream[off:], buf)
		if err != nil {
			t.Fatalf("%v: decode at %d: %v", c, off, err)
		}
		if got != c {
			t.Fatalf("decoded header %v, encoded %v", got, c)
		}
		out = append(out, buf...)
		off += n
	}
	return out, stream
}

// snr compares out to in per channel at the delay that fits best, skipping
// the filters' start-up, and returns the worst channel's SNR in dB and the
// gain at that delay.
func snr(in, out []int16, ch, skip int) (worst, gain float64, delay int) {
	worst = math.Inf(1)
	n := len(in) / ch
	bestDelay := 0
	bestErr := math.Inf(1)
	for d := 0; d < 200 && d < n-skip; d++ {
		var e float64
		for t := skip; t+d < n; t++ {
			for i := 0; i < ch; i++ {
				diff := float64(out[(t+d)*ch+i]) - float64(in[t*ch+i])
				e += diff * diff
			}
		}
		if e < bestErr {
			bestErr, bestDelay = e, d
		}
	}
	for i := 0; i < ch; i++ {
		var sig, noise, xy, xx float64
		for t := skip; t+bestDelay < n; t++ {
			x, y := float64(in[t*ch+i]), float64(out[(t+bestDelay)*ch+i])
			sig += x * x
			noise += (y - x) * (y - x)
			xy += x * y
			xx += x * x
		}
		r := 10 * math.Log10(sig/math.Max(noise, 1e-9))
		if sig == 0 {
			r = math.Inf(1)
			if noise > 0 {
				r = -10 * math.Log10(noise)
			}
		}
		if r < worst {
			worst = r
		}
		if xx > 0 {
			gain = xy / xx
		}
	}
	return worst, gain, bestDelay
}

// allConfigs is every header the spec allows, at the given bitpools (as
// functions of the mode's maximum).
func allConfigs(bitpools func(max int) []int) []Config {
	var out []Config
	for _, rate := range []int{16000, 32000, 44100, 48000} {
		for _, mode := range []ChannelMode{Mono, DualChannel, Stereo, JointStereo} {
			for _, blocks := range []int{4, 8, 12, 16} {
				for _, nsb := range []int{4, 8} {
					for _, alloc := range []Allocation{Loudness, SNR} {
						for _, bp := range bitpools(MaxBitpool(mode, nsb)) {
							out = append(out, Config{rate, mode, blocks, nsb, alloc, bp})
						}
					}
				}
			}
		}
	}
	return out
}

// Every configuration encodes and decodes: frames are FrameLength long,
// carry their own header, and at the largest bitpool reproduce the input.
func TestEveryConfigRoundTrips(t *testing.T) {
	cfgs := allConfigs(func(max int) []int { return []int{2, max / 2, max} })
	if len(cfgs) != 768 {
		t.Fatalf("%d configurations, want 768", len(cfgs))
	}
	for _, c := range cfgs {
		for _, kind := range []string{"sine", "noise"} {
			frames := 2048/c.SamplesPerFrame() + 1
			in := signal(kind, c, frames, 3)
			out, stream := roundTrip(t, c, in)
			if len(stream) != frames*c.FrameLength() {
				t.Fatalf("%v: %d bytes for %d frames of %d", c, len(stream), frames, c.FrameLength())
			}
			if c.Bitpool != MaxBitpool(c.Mode, c.Subbands) {
				continue
			}
			s, gain, delay := snr(in, out, c.Channels(), 200)
			if want := 9*c.Subbands + 1; delay != want || s < 35 || gain < 0.99 || gain > 1.01 {
				t.Errorf("%v %s: %.1f dB gain %.3f delay %d, want >= 35 dB at unity, delay %d",
					c, kind, s, gain, delay, want)
			}
		}
	}
}

// Digital silence decodes to exact zeros: each quantiser has a level at 0.
func TestSilenceIsExact(t *testing.T) {
	for _, c := range allConfigs(func(max int) []int { return []int{2, max} }) {
		in := signal("silence", c, 3, 0)
		out, _ := roundTrip(t, c, in)
		for i, v := range out {
			if v != 0 {
				t.Fatalf("%v: sample %d = %d from silence", c, i, v)
			}
		}
	}
}

// A full-scale square wave saturates the largest scale factor without
// wrapping a quantiser.
func TestFullScaleDoesNotWrap(t *testing.T) {
	c := Config{44100, JointStereo, 16, 8, Loudness, 53}
	in := make([]int16, c.SamplesPerFrame()*2*20)
	for i := range in {
		if (i/2/50)&1 == 0 {
			in[i] = 32767
		} else {
			in[i] = -32768
		}
	}
	out, _ := roundTrip(t, c, in)
	if s, _, _ := snr(in, out, 2, 200); s < 20 {
		t.Errorf("full-scale square: %.1f dB", s)
	}
}

func TestEncodeAllocatesNothing(t *testing.T) {
	c := Config{48000, JointStereo, 16, 8, Loudness, 53}
	enc, _ := NewEncoder(c)
	pcm := signal("noise", c, 1, 1)
	dst := make([]byte, c.FrameLength())
	if n := testing.AllocsPerRun(100, func() { enc.Encode(dst, pcm) }); n != 0 {
		t.Errorf("Encode allocates %v times per frame", n)
	}
}

func TestEncodeRejectsWrongSizes(t *testing.T) {
	c := Config{44100, Stereo, 16, 8, Loudness, 53}
	enc, _ := NewEncoder(c)
	if _, err := enc.Encode(make([]byte, c.FrameLength()), make([]int16, 255)); err == nil {
		t.Error("255 samples accepted for a 256-sample frame")
	}
	if _, err := enc.Encode(make([]byte, c.FrameLength()-1), make([]int16, 256)); err == nil {
		t.Error("a short destination accepted")
	}
	if _, err := NewEncoder(Config{44100, Stereo, 16, 8, Loudness, 251}); err == nil {
		t.Error("bitpool 251 accepted")
	}
}

func TestDecodeRejectsDamage(t *testing.T) {
	c := Config{44100, JointStereo, 16, 8, Loudness, 53}
	_, stream := roundTrip(t, c, signal("sine", c, 1, 1))
	pcm := make([]int16, 256)
	bad := append([]byte(nil), stream...)
	bad[0] = 0x9D
	if _, _, err := NewDecoder().Decode(bad, pcm); err != ErrSync {
		t.Errorf("bad syncword: %v", err)
	}
	bad = append([]byte(nil), stream...)
	bad[5] ^= 0x10 // a scale factor bit, covered by the CRC
	if _, _, err := NewDecoder().Decode(bad, pcm); err != ErrCRC {
		t.Errorf("damaged scale factor: %v", err)
	}
	if _, _, err := NewDecoder().Decode(stream[:len(stream)-1], pcm); err == nil {
		t.Error("a truncated frame decoded")
	}
	if _, _, err := NewDecoder().Decode(stream, pcm[:255]); err == nil {
		t.Error("a short pcm buffer accepted")
	}
}
