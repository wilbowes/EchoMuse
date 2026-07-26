package beamformer

import (
	"bytes"
	"math"
	"math/rand"
	"testing"
)

// buildRaw creates an interleaved S24_3LE buffer with the given channel count,
// filling each channel with distinct pseudo-random samples.
func buildRaw(t *testing.T, channels, frames int, seed int64) []byte {
	t.Helper()
	r := rand.New(rand.NewSource(seed))
	raw := make([]byte, frames*channels*3)
	for f := 0; f < frames; f++ {
		for c := 0; c < channels; c++ {
			v := int32(r.Intn(1<<24)) - (1 << 23) // full signed 24-bit range
			o := (f*channels + c) * 3
			raw[o] = byte(v)
			raw[o+1] = byte(v >> 8)
			raw[o+2] = byte(v >> 16)
		}
	}
	return raw
}

// TestBypassMatchesExtractChannel is the load-bearing test for the port: the
// Echo Show 5 path must produce bit-identical output to the existing Echo Dot
// path for the same channel, so downstream levels, clipping and telemetry are
// unchanged by swapping in the bypass.
func TestBypassMatchesExtractChannel(t *testing.T) {
	const frames = 512
	raw := buildRaw(t, nChannels, frames, 42)

	for _, gain := range []float64{0.5, 1.0, 2.0, 8.0} {
		b := New()
		want := b.extractChannel(raw, centreCh, gain)

		bp := NewBypass(nChannels, centreCh)
		got, angle := bp.Process(raw, 0, gain)

		if !bytes.Equal(want, got) {
			t.Fatalf("gain %v: bypass output differs from extractChannel", gain)
		}
		if angle != -1 {
			t.Errorf("gain %v: angle = %v, want -1 (no estimate)", gain, angle)
		}
		if b.ClippedSamples() != bp.ClippedSamples() {
			t.Errorf("gain %v: clipped %d vs bypass %d",
				gain, b.ClippedSamples(), bp.ClippedSamples())
		}
	}
}

// TestBypassGainUnityIsTopTwoBytes pins the documented invariant that gain 1.0
// reproduces the "upper two bytes of the 24-bit sample" behaviour exactly.
func TestBypassGainUnityIsTopTwoBytes(t *testing.T) {
	const channels = 4
	raw := buildRaw(t, channels, 128, 7)

	got, _ := NewBypass(channels, 0).Process(raw, 0, 1.0)
	for i := 0; i < 128; i++ {
		o := i * channels * 3
		want := int16(uint16(raw[o+1]) | uint16(raw[o+2])<<8)
		have := int16(uint16(got[i*2]) | uint16(got[i*2+1])<<8)
		if want != have {
			t.Fatalf("frame %d: got %d want %d", i, have, want)
		}
	}
}

// TestBypassClipsAndCounts checks saturation rather than wraparound, which
// would turn loud speech into noise.
func TestBypassClipsAndCounts(t *testing.T) {
	const channels = 4
	frames := 16
	raw := make([]byte, frames*channels*3)
	for f := 0; f < frames; f++ {
		v := int32(0x7FFFFF) // near full scale positive
		o := f * channels * 3
		raw[o], raw[o+1], raw[o+2] = byte(v), byte(v>>8), byte(v>>16)
	}

	bp := NewBypass(channels, 0)
	got, _ := bp.Process(raw, 0, 64.0) // guaranteed to saturate

	for i := 0; i < frames; i++ {
		have := int16(uint16(got[i*2]) | uint16(got[i*2+1])<<8)
		if have != math.MaxInt16 {
			t.Fatalf("frame %d: got %d, want saturation to %d", i, have, math.MaxInt16)
		}
	}
	if bp.ClippedSamples() != uint64(frames) {
		t.Errorf("clipped = %d, want %d", bp.ClippedSamples(), frames)
	}
}

// TestBypassMixAveragesChannels covers the two-mic sum path.
func TestBypassMixAveragesChannels(t *testing.T) {
	const channels = 4
	frames := 4
	raw := make([]byte, frames*channels*3)
	put := func(f, c int, v int32) {
		o := (f*channels + c) * 3
		raw[o], raw[o+1], raw[o+2] = byte(v), byte(v>>8), byte(v>>16)
	}
	for f := 0; f < frames; f++ {
		put(f, 0, 1000<<8)
		put(f, 1, 3000<<8)
	}

	got, _ := NewBypassMix(channels, []int{0, 1}).Process(raw, 0, 1.0)
	for i := 0; i < frames; i++ {
		have := int16(uint16(got[i*2]) | uint16(got[i*2+1])<<8)
		if have != 2000 {
			t.Fatalf("frame %d: got %d, want mean 2000", i, have)
		}
	}
}
