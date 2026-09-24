package speaker

import (
	"encoding/binary"
	"math"
	"testing"
)

// The law is the DAC control's, so every level plays at the loudness it did
// when the DAC applied it: 0.5dB per step, unity at 127.
func TestVolumeGainIsTheCodecLaw(t *testing.T) {
	for _, tc := range []struct {
		level int
		want  float64
	}{
		{127, 1.0},
		{87, 0.1},   // -20dB
		{47, 0.01},  // -40dB, the button floor
		{175, 1.0},  // never above unity: the DAC saturated there
		{0, 0},      // HA's 0.0 is silence
		{-3, 0},
	} {
		if got := VolumeGain(tc.level); math.Abs(got-tc.want) > tc.want*0.001+1e-12 {
			t.Errorf("level %d: gain %.6f, want %.6f", tc.level, got, tc.want)
		}
	}
}

func testPeriod(v int16) []byte {
	b := make([]byte, 2048*4) // one period: 2048 stereo S16 frames
	for i := 0; i+1 < len(b); i += 2 {
		binary.LittleEndian.PutUint16(b[i:], uint16(v))
	}
	return b
}

func sampleAtFrame(b []byte, frame, ch int) int16 {
	return int16(binary.LittleEndian.Uint16(b[frame*4+ch*2:]))
}

// A new volume ramps across one period and then holds: no step mid-waveform,
// and the next period is exactly at the target.
func TestSoftVolumeRampsThenHolds(t *testing.T) {
	var v softVolume
	v.set(1)
	v.settle()
	v.set(0.5)

	p := testPeriod(20000)
	v.apply(p)
	frames := len(p) / 4
	first, last := sampleAtFrame(p, 0, 0), sampleAtFrame(p, frames-1, 1)
	if first < 19990 {
		t.Errorf("ramp starts at %d, want ~20000 — the change landed as a step", first)
	}
	if last != 10000 {
		t.Errorf("ramp ends at %d, want 10000", last)
	}
	prev := int16(math.MaxInt16)
	for i := 0; i < frames; i++ {
		s := sampleAtFrame(p, i, 0)
		if s > prev {
			t.Fatalf("ramp not monotonic at frame %d", i)
		}
		prev = s
	}

	p = testPeriod(20000)
	v.apply(p)
	if a, b := sampleAtFrame(p, 0, 0), sampleAtFrame(p, frames-1, 0); a != 10000 || b != 10000 {
		t.Errorf("held period reads %d..%d, want 10000 throughout", a, b)
	}
}

// Unset is silence, never full scale.
func TestSoftVolumeZeroValueIsSilent(t *testing.T) {
	var v softVolume
	p := testPeriod(20000)
	v.apply(p)
	for i := 0; i < len(p)/4; i++ {
		if s := sampleAtFrame(p, i, 0); s != 0 {
			t.Fatalf("frame %d is %d with no volume set", i, s)
		}
	}
}
