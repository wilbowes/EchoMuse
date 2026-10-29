package speaker

import (
	"encoding/binary"
	"testing"
)

func TestScaleS16(t *testing.T) {
	src := make([]byte, 8)
	for i, v := range []int16{1000, -1000, 30000, -30000} {
		binary.LittleEndian.PutUint16(src[i*2:], uint16(v))
	}
	dst := make([]byte, 8)
	scaleS16(dst, src, 0.5)
	want := []int16{500, -500, 15000, -15000}
	for i, w := range want {
		if got := int16(binary.LittleEndian.Uint16(dst[i*2:])); got != w {
			t.Errorf("x0.5 sample %d: %d want %d", i, got, w)
		}
	}
	scaleS16(dst, src, 2)
	for i, w := range []int16{2000, -2000, 32767, -32768} {
		if got := int16(binary.LittleEndian.Uint16(dst[i*2:])); got != w {
			t.Errorf("x2 sample %d: %d want %d (saturating)", i, got, w)
		}
	}
	scaleS16(dst, src, 1)
	if string(dst) != string(src) {
		t.Error("unity gain must copy exactly")
	}
	scaleS16(dst, src, 0)
	for _, b := range dst {
		if b != 0 {
			t.Fatal("zero gain must be silence")
		}
	}
}
