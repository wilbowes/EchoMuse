package outchain

import (
	"bytes"
	"encoding/binary"
	"math"
	"testing"
)

// shaped is a chain doing real work on every stage: boosted EQ (so the
// sections carry state), the bass guard engaged by a loud low band, and a
// limiter low enough to reduce.
func shaped() *Chain {
	c := New(48000)
	c.SetActive(true)
	p := DefaultParams()
	p.Bands = [NumBands]float64{9, 6, 0, -3, 0, 3, 6, 9}
	p.GuardEnabled, p.GuardDb = true, -12
	p.LimiterEnabled, p.LimiterThresholdDb = true, -6
	c.SetParams(p)
	return c
}

// interleave builds a stereo S16_LE period from two channel generators.
func interleave(frames, start int, l, r func(i int) float64) []byte {
	b := make([]byte, frames*4)
	for i := 0; i < frames; i++ {
		binary.LittleEndian.PutUint16(b[i*4:], uint16(int16(l(start+i))))
		binary.LittleEndian.PutUint16(b[i*4+2:], uint16(int16(r(start+i))))
	}
	return b
}

func tone(amp, hz float64) func(int) float64 {
	return func(i int) float64 { return amp * math.Sin(2*math.Pi*hz*float64(i)/48000) }
}

func silence(int) float64 { return 0 }

func channel(b []byte, ch int) []int16 {
	out := make([]int16, len(b)/4)
	for i := range out {
		out[i] = int16(binary.LittleEndian.Uint16(b[i*4+ch*2:]))
	}
	return out
}

// The switch to stereo processing must be inaudible: on dual-mono audio the
// stereo path computes exactly what the mono path does, so a chain that has
// been stereo from the first sample and one that switches on the first
// differing period agree bit for bit, before and after the switch.
func TestSwitchToStereoIsBitExact(t *testing.T) {
	mono, forced := shaped(), shaped()
	// One silent period each, so activation's reset is behind them, then the
	// second goes stereo from zero state — what a split would copy.
	mono.Process(make([]byte, 2048*4))
	forced.Process(make([]byte, 2048*4))
	forced.stereo = true

	bass := tone(20000, 60)
	for p := 0; p < 6; p++ {
		a := interleave(2048, p*2048, bass, bass)
		b := append([]byte(nil), a...)
		mono.Process(a)
		forced.Process(b)
		if mono.stereo {
			t.Fatalf("period %d: dual-mono audio switched the chain to stereo", p)
		}
		if !bytes.Equal(a, b) {
			t.Fatalf("period %d: the stereo path differs from the mono path on L == R", p)
		}
	}

	// Now the switch itself, mid-stream with every filter carrying state.
	a := interleave(2048, 6*2048, bass, tone(9000, 1000))
	b := append([]byte(nil), a...)
	mono.Process(a)
	forced.Process(b)
	if !mono.stereo {
		t.Fatal("did not switch on the first period with L != R")
	}
	if !bytes.Equal(a, b) {
		t.Fatal("switching mid-stream is not the same as having been stereo all along")
	}
}

// What is in one channel stays in that channel.
func TestStereoKeepsTheChannelsApart(t *testing.T) {
	c := New(48000)
	c.SetActive(true) // defaults: flat EQ, guard and limiter idle at this level
	for p := 0; p < 4; p++ {
		buf := interleave(2048, p*2048, tone(8000, 440), silence)
		c.Process(buf)
		for i, s := range channel(buf, 1) {
			if s != 0 {
				t.Fatalf("period %d frame %d: the right channel carries %d of the left", p, i, s)
			}
		}
	}
}

// One gain for both sides: a peak on the left pulls the right down by the
// same amount, so a limited passage does not shift towards the quiet side.
func TestLimiterIsLinked(t *testing.T) {
	c := New(48000)
	c.SetActive(true)
	p := DefaultParams()
	p.LimiterEnabled, p.LimiterThresholdDb = true, -12
	c.SetParams(p)
	var l, r []int16
	for k := 0; k < 4; k++ {
		buf := interleave(2048, k*2048, func(int) float64 { return 30000 }, func(int) float64 { return 3000 })
		c.Process(buf)
		l, r = channel(buf, 0), channel(buf, 1)
	}
	last := len(l) - 1
	if l[last] >= 30000 {
		t.Fatalf("the left was not limited: %d", l[last])
	}
	ratioL := float64(l[last]) / 30000
	ratioR := float64(r[last]) / 3000
	if math.Abs(ratioL-ratioR) > 0.002 {
		t.Errorf("unlinked: left at %.4f of input, right at %.4f", ratioL, ratioR)
	}
}

// Silence resets the chain, and with it the stereo state: what follows is
// processed as mono again until it shows otherwise.
func TestStereoEndsWithTheResetOnSilence(t *testing.T) {
	c := shaped()
	c.Process(interleave(2048, 0, tone(9000, 440), silence))
	if !c.stereo {
		t.Fatal("not stereo")
	}
	for n := 0; !c.Idle(); n++ {
		if n > 200 {
			t.Fatal("never went idle on silence")
		}
		c.Process(make([]byte, 2048*4))
	}
	if c.stereo {
		t.Error("still stereo after the reset")
	}
}
