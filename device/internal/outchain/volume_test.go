package outchain

import (
	"encoding/binary"
	"math"
	"testing"
)

func tone(frames int, hz, amp float64) []byte {
	buf := make([]byte, frames*4)
	for i := 0; i < frames; i++ {
		s := int16(amp * math.Sin(2*math.Pi*hz*float64(i)/48000))
		binary.LittleEndian.PutUint16(buf[i*4:], uint16(s))
		binary.LittleEndian.PutUint16(buf[i*4+2:], uint16(s))
	}
	return buf
}

func rmsOf(buf []byte) float64 {
	var sum float64
	n := len(buf) / 4
	for i := 0; i < n; i++ {
		v := float64(int16(binary.LittleEndian.Uint16(buf[i*4:])))
		sum += v * v
	}
	return math.Sqrt(sum / float64(n))
}

// Only Radar takes the volume ahead of its chain; every other board leaves
// it to the speaker, after the chain, as before.
func TestOnlyRadarTakesVolume(t *testing.T) {
	if !NewForBoard(48000, "radar", nil).TakesVolume() {
		t.Error("radar chain does not take the volume")
	}
	for _, id := range []string{"biscuit", ""} {
		if NewForBoard(48000, id, nil).TakesVolume() {
			t.Errorf("%q chain takes the volume", id)
		}
	}
}

// The point of taking the volume first: a loud 100Hz tone that band 2's
// 10:1 compressor holds down at full scale passes untouched once the volume
// has brought it under the threshold — so turning the volume down by 20dB
// lowers the bass by LESS than 20dB, the way stock's does. Applied after the
// chain, as before, it would be exactly 20dB at every level.
func TestRadarVolumeAheadOfMBCL(t *testing.T) {
	run := func(gain float64) float64 {
		c := NewForBoard(48000, "radar", radarTuningForTest(t))
		c.SetActive(true)
		c.SetVolumeGain(gain)
		var out float64
		for k := 0; k < 12; k++ {
			buf := tone(2048, 100, 20000)
			c.Process(buf)
			out = rmsOf(buf)
		}
		return out
	}
	full, quiet := run(1), run(0.1)
	drop := 20 * math.Log10(full/quiet)
	if drop >= 19 {
		t.Errorf("20dB of volume dropped the compressed bass by %.1fdB; want well under 20", drop)
	}
	t.Logf("20dB of volume -> %.1fdB at the output", drop)
}

// Volume zero is silence through a chain that takes it, and a biscuit chain
// ignores SetVolumeGain entirely.
func TestVolumeGainEdges(t *testing.T) {
	c := NewForBoard(48000, "radar", radarTuningForTest(t))
	c.SetActive(true)
	c.SetVolumeGain(0)
	buf := tone(2048, 1000, 20000)
	c.Process(buf) // ramps to 0 across the first period
	buf = tone(2048, 1000, 20000)
	c.Process(buf)
	if r := rmsOf(buf); r > 1 {
		t.Errorf("radar at volume 0: rms %.2f, want silence", r)
	}

	b := NewForBoard(48000, "biscuit", nil)
	b.SetActive(true)
	b.SetParams(Params{}) // everything off: a passthrough
	b.SetVolumeGain(0)
	in := tone(2048, 1000, 20000)
	for k := 0; k < 2; k++ { // the second, once the limiter's look-ahead has filled
		buf = append([]byte(nil), in...)
		b.Process(buf)
	}
	if rmsOf(buf) < rmsOf(in)*0.99 {
		t.Error("biscuit chain applied a volume it does not take")
	}
}

// TookVolume answers for what Process did: never while inactive, and never
// on a board that does not take the volume.
func TestTookVolume(t *testing.T) {
	r := NewForBoard(48000, "radar", radarTuningForTest(t))
	r.Process(tone(2048, 1000, 1000))
	if r.TookVolume() {
		t.Error("inactive radar chain reports it took the volume")
	}
	r.SetActive(true)
	r.Process(tone(2048, 1000, 1000))
	if !r.TookVolume() {
		t.Error("active radar chain reports it did not take the volume")
	}
	b := NewForBoard(48000, "biscuit", nil)
	b.SetActive(true)
	b.Process(tone(2048, 1000, 1000))
	if b.TookVolume() {
		t.Error("biscuit chain reports it took the volume")
	}
}

// Every stock volume value survives the trip to a device level and back,
// and the levels are stock's (value + 27 from 11 up, 127 at 100).
func TestStockVolumeRoundTrips(t *testing.T) {
	for v := 0; v <= 100; v++ {
		if got := StockVolumeValue(StockVolumeLevel(v)); got != v {
			t.Errorf("value %d -> level %d -> value %d", v, StockVolumeLevel(v), got)
		}
	}
	if StockVolumeLevel(30) != 57 || StockVolumeLevel(100) != 127 || StockVolumeLevel(5) != 20 {
		t.Errorf("levels 30/100/5 = %d/%d/%d, want 57/127/20",
			StockVolumeLevel(30), StockVolumeLevel(100), StockVolumeLevel(5))
	}
}
