package aec

import (
	"encoding/binary"
	"math"
	"testing"
)

func benchFrames(n int) ([]byte, []byte) {
	mic := make([]byte, n*2)
	ref := make([]byte, n*2)
	for i := 0; i < n; i++ {
		m := int16(3000 * math.Sin(float64(i)*0.05))
		r := int16(8000 * math.Sin(float64(i)*0.05+0.3))
		binary.LittleEndian.PutUint16(mic[i*2:], uint16(m))
		binary.LittleEndian.PutUint16(ref[i*2:], uint16(r))
	}
	return mic, ref
}

// One 512-sample (32ms) period through one canceller at the shipped tail.
func BenchmarkCancelOnePeriod300ms(b *testing.B) {
	c := New()
	c.SetParams(true, 0, 300)
	c.SetHardwareRef(true)
	mic, ref := benchFrames(FrameSize)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		c.ProcessWithRef(mic, ref)
	}
}

func BenchmarkCancelOnePeriod150ms(b *testing.B) {
	c := New()
	c.SetParams(true, 0, 150)
	c.SetHardwareRef(true)
	mic, ref := benchFrames(FrameSize)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		c.ProcessWithRef(mic, ref)
	}
}

// Seven cancellers, one per microphone, on the same period — the
// "AEC before the beamformer" architecture.
func BenchmarkCancelSevenMics300ms(b *testing.B) {
	var cs [7]*Canceller
	for i := range cs {
		cs[i] = New()
		cs[i].SetParams(true, 0, 300)
		cs[i].SetHardwareRef(true)
	}
	mic, ref := benchFrames(FrameSize)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		for _, c := range cs {
			c.ProcessWithRef(mic, ref)
		}
	}
}
