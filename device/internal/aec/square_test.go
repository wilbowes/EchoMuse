package aec

import (
	"math"
	"testing"
)

// squareRef against its definition, every input: x²/32768 rounded half to
// even (lrint, the offline harness the gain was measured with) and
// saturated to int16. Only -32768 saturates.
func TestSquareRefIsRoundedHalfEvenAndSaturated(t *testing.T) {
	for v := math.MinInt16; v <= math.MaxInt16; v++ {
		want := math.RoundToEven(float64(v) * float64(v) / 32768)
		if want > 32767 {
			want = 32767
		}
		if got := squareRef(int16(v)); float64(got) != want {
			t.Fatalf("squareRef(%d) = %d, want %v", v, got, want)
		}
	}
	// The edges by name, so a failure above reads against these.
	for _, c := range []struct{ x, want int16 }{
		{0, 0}, {1, 0}, {-1, 0},
		{128, 0}, // 0.5 rounds to even 0
		{384, 4}, // 4.5 rounds to even 4
		{181, 1}, // 0.9998
		{32767, 32766},
		{-32768, 32767}, // 32768 saturates
	} {
		if got := squareRef(c.x); got != c.want {
			t.Errorf("squareRef(%d) = %d, want %d", c.x, got, c.want)
		}
	}
}

// micEchoDistorted is micEcho plus an even-order term: the same per-mic
// path applied to x², DC removed (a speaker's second harmonic is AC), at
// distDb below the linear echo. That is the Dot 2's residual in shape.
func micEchoDistorted(signal []int16, ch int, distDb float64) []int16 {
	sq := make([]int16, len(signal))
	var mean float64
	for i, x := range signal {
		sq[i] = squareRef(x)
		mean += float64(sq[i])
	}
	mean /= float64(len(sq))
	lin := micEcho(signal, ch)
	ac := make([]int16, len(sq))
	for i := range sq {
		ac[i] = int16(float64(sq[i]) - mean)
	}
	dist := micEcho(ac, ch)
	g := rms(toBytes(lin)) / rms(toBytes(dist)) * math.Pow(10, -distDb/20)
	out := make([]int16, len(signal))
	for i := range out {
		out[i] = int16(float64(lin[i]) + g*float64(dist[i]))
	}
	return out
}

func linearCanceller(tailMs int) *Canceller {
	c := New(NumMics)
	c.linearOnly = true
	c.SetParams(true, 0, tailMs)
	c.SetHardwareRef(true)
	return c
}

// The square reference must remove an even-order echo the linear filter
// cannot, and must cost nothing on an echo with none. The first half is
// what a wrong expansion (x copied into the second channel, or the
// channels' interleaving broken) fails; the second is what an expansion
// that destabilised the filter fails.
func TestSquareReferenceCancelsEvenOrderEcho(t *testing.T) {
	const learn, score = 150, 50
	// At reply level: synth() is about -24dBFS, where x² is 20dB under x and,
	// with speex normalising the step by the channels' summed power, learns
	// slowly. A reply at volume 89 is around -10dBFS (x² about 6dB under x).
	signal := synth((learn + score) * FrameSize)
	for i, v := range signal {
		signal[i] = int16(max(-32768, min(32767, 4*int32(v))))
	}
	// Room noise ~30dB under the echo, so both filters stop at a depth a
	// room allows and not at the 50-60dB a noise-free synthetic echo reaches.
	noise := synth((learn + score) * FrameSize)
	for i := range noise {
		noise[i] = noise[(i*7919)%len(noise)] / 12
	}

	for _, tc := range []struct {
		name    string
		distort bool
	}{{"even-order distortion 15dB down", true}, {"linear echo only", false}} {
		echoes := make([][]int16, NumMics)
		for ch := range echoes {
			if tc.distort {
				echoes[ch] = micEchoDistorted(signal, ch, 15)
			} else {
				echoes[ch] = micEcho(signal, ch)
			}
			for i := range echoes[ch] {
				echoes[ch][i] += noise[(i+ch*1031)%len(noise)]
			}
		}
		sq, lin := hwCanceller(hwTailMs), linearCanceller(hwTailMs)
		if sq.farK != hwFarChannels || lin.farK != 1 {
			t.Fatalf("far channels: %d and %d, want %d and 1", sq.farK, lin.farK, hwFarChannels)
		}
		runMics(sq, signal, echoes, 6, 0, learn)
		runMics(lin, signal, echoes, 6, 0, learn)
		got := runMics(sq, signal, echoes, 6, learn, learn+score)
		want := runMics(lin, signal, echoes, 6, learn, learn+score)
		t.Logf("%s: square reference %.1fdB, linear %.1fdB", tc.name, got, want)
		if tc.distort && got < want+6 {
			t.Errorf("%s: %.1fdB with the square reference, %.1fdB without: want 6dB more", tc.name, got, want)
		}
		if !tc.distort && got < want-1 {
			t.Errorf("%s: %.1fdB with the square reference, %.1fdB without: it must not cost a linear echo", tc.name, got, want)
		}
	}
}

// The software tap stays on one channel: its alignment error is larger
// than anything the square term could win, and it was never measured.
func TestSoftwareTapKeepsOneFarChannel(t *testing.T) {
	c := New(NumMics)
	c.SetParams(true, 0, 64)
	if c.farK != 1 {
		t.Fatalf("software tap built with %d far channels, want 1", c.farK)
	}
}
