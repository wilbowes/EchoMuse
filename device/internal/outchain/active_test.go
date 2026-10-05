package outchain

import (
	"bytes"
	"testing"
)

func loud(n int) []byte {
	m := make([]int16, n)
	for i := range m {
		m[i] = int16((i%97)*300 - 14000)
	}
	return stereo(m)
}

// Inactive is the state every device starts in and stays in under a
// controller that still processes. It must not touch a single byte.
func TestInactiveIsBitExactPassthrough(t *testing.T) {
	c := New(48000)
	p := DefaultParams()
	p.Bands[0] = 12
	c.SetParams(p)
	in := loud(2048)
	buf := append([]byte(nil), in...)
	c.Process(buf)
	if !bytes.Equal(buf, in) {
		t.Fatal("an inactive chain changed the audio")
	}
	if !c.Idle() {
		t.Fatal("an inactive chain must report idle so silence is skipped")
	}
}

// After audio stops, the chain processes silence only until its tails have
// decayed, then goes idle — otherwise it runs flat out on an idle speaker.
func TestGoesIdleAfterTailsDecay(t *testing.T) {
	c := New(48000)
	c.SetActive(true)
	p := DefaultParams()
	p.Bands = [NumBands]float64{12, 12, 12, 12, 12, 12, 12, 12}
	c.SetParams(p)
	c.Process(loud(2048))
	if c.Idle() {
		t.Fatal("idle straight after audio")
	}
	periods := 0
	for !c.Idle() {
		if periods++; periods > 200 {
			t.Fatal("never went idle on silence")
		}
		c.Process(make([]byte, 2048*4))
	}
	t.Logf("idle after %d silent periods", periods)

	// And an idle chain wakes on the next audio.
	c.Process(loud(2048))
	if c.Idle() {
		t.Fatal("stayed idle through audio")
	}
}

// Handing the chain over mid-stream must start clean: state learnt while
// inactive would be state for audio the chain never processed.
func TestActivationResetsState(t *testing.T) {
	a := New(48000)
	a.SetActive(true)
	b := New(48000)
	b.Process(loud(2048)) // inactive: must learn nothing
	b.SetActive(true)
	x, y := loud(2048), loud(2048)
	a.Process(x)
	b.Process(y)
	if !bytes.Equal(x, y) {
		t.Fatal("activation carried state from an inactive period")
	}
}
