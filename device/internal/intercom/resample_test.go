package intercom

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func s16(samples ...int16) []byte {
	b := make([]byte, len(samples)*2)
	for i, s := range samples {
		binary.LittleEndian.PutUint16(b[i*2:], uint16(s))
	}
	return b
}

func got16(t *testing.T, b []byte) []int16 {
	t.Helper()
	if len(b)%2 != 0 {
		t.Fatalf("output length %d is not whole samples", len(b))
	}
	out := make([]int16, len(b)/2)
	for i := range out {
		out[i] = int16(binary.LittleEndian.Uint16(b[i*2:]))
	}
	return out
}

func TestResamplerSilenceStaysSilence(t *testing.T) {
	r := &Resampler{}
	out := r.Write(s16(0, 0, 0, 0))
	if got := got16(t, out); len(got) != 9 {
		t.Fatalf("silence: got %d samples, want 9", len(got))
	} else {
		for i, v := range got {
			if v != 0 {
				t.Fatalf("silence: sample %d = %d, want 0", i, v)
			}
		}
	}
}

func TestResamplerConstantStaysConstant(t *testing.T) {
	r := &Resampler{}
	out := r.Write(s16(1000, 1000, 1000))
	if got := got16(t, out); len(got) != 6 {
		t.Fatalf("constant: got %d samples, want 6", len(got))
	} else {
		for i, v := range got {
			if v != 1000 {
				t.Fatalf("constant: sample %d = %d, want 1000", i, v)
			}
		}
	}
}

func TestResamplerStepInterpolates(t *testing.T) {
	r := &Resampler{}
	out := r.Write(s16(0, 3000))
	// The frame's last sample is held: only the first sample's three
	// outputs appear (0, then 1000 and 2000 toward 3000).
	want := []int16{0, 1000, 2000}
	if got := got16(t, out); !equal16(got, want) {
		t.Fatalf("step: got %v, want %v", got, want)
	}
	// The held sample flushes as a hold.
	if got := got16(t, r.Flush()); !equal16(got, []int16{3000, 3000, 3000}) {
		t.Fatalf("flush: got %v, want [3000 3000 3000]", got)
	}
}

func TestResamplerSplitFramesMatchOneFrame(t *testing.T) {
	one := &Resampler{}
	a := one.Write(s16(0, 100, 200, 300))
	a = append(a, one.Flush()...)

	two := &Resampler{}
	b := two.Write(s16(0, 100))
	b = append(b, two.Write(s16(200, 300))...)
	b = append(b, two.Flush()...)

	if !bytes.Equal(a, b) {
		t.Fatalf("split frames differ:\n one: %v\n two: %v", got16(t, a), got16(t, b))
	}
	if got := got16(t, a); !equal16(got, []int16{0, 33, 66, 100, 133, 166, 200, 233, 266, 300, 300, 300}) {
		t.Fatalf("ramp: got %v", got)
	}
}

func TestResamplerDropsOddTrailingByte(t *testing.T) {
	r := &Resampler{}
	out := r.Write(append(s16(0, 3000), 0xFF))
	if got := got16(t, out); !equal16(got, []int16{0, 1000, 2000}) {
		t.Fatalf("odd byte: got %v, want [0 1000 2000]", got)
	}
}

func TestResamplerEmptyAndFlushEmpty(t *testing.T) {
	r := &Resampler{}
	if out := r.Write(nil); out != nil {
		t.Fatalf("empty Write: got %v, want nil", out)
	}
	if out := r.Flush(); out != nil {
		t.Fatalf("empty Flush: got %v, want nil", out)
	}
	r.Reset()
	if out := r.Flush(); out != nil {
		t.Fatalf("Flush after Reset: got %v, want nil", out)
	}
}

func equal16(a, b []int16) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
