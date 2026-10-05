package speaker

import (
	"math"
	"testing"
)

// period builds a stereo S16 period where every frame has the same value.
func period(frames int, v int16) []byte {
	b := make([]byte, frames*4)
	for i := 0; i < frames; i++ {
		for c := 0; c < 2; c++ {
			off := i*4 + c*2
			b[off] = byte(uint16(v) & 0xff)
			b[off+1] = byte(uint16(v) >> 8)
		}
	}
	return b
}

func sampleAt(b []byte, frame, ch int) int16 {
	off := frame*4 + ch*2
	return int16(uint16(b[off]) | uint16(b[off+1])<<8)
}

func TestDuckGainUnityIsExact(t *testing.T) {
	// A stream that is not ducked must be bit-identical, not "close".
	if g := DuckGain(0); g != unityGain {
		t.Fatalf("0dB should be exactly unity, got %d", g)
	}
	m := &Mixer{gain: unityGain}
	in := period(4, 12345)
	m.applyGain(in, unityGain)
	for i := 0; i < 4; i++ {
		if got := sampleAt(in, i, 0); got != 12345 {
			t.Fatalf("unity gain altered the sample: %d", got)
		}
	}
}

func TestDuckGainDecibels(t *testing.T) {
	// -6dB is half amplitude, -18dB is the starting duck depth.
	for _, tc := range []struct{ db, want float64 }{
		{-6, 0.5012}, {-18, 0.1259}, {-60, 0.001},
	} {
		got := float64(DuckGain(tc.db)) / float64(unityGain)
		if math.Abs(got-tc.want) > 0.001 {
			t.Errorf("%gdB: got %.4f want %.4f", tc.db, got, tc.want)
		}
	}
}

func TestVoiceAloneIsNeverAltered(t *testing.T) {
	// The response is the thing being listened to; ducking applies to the
	// bed underneath it, never to it.
	m := &Mixer{gain: DuckGain(-18)}
	voice := period(4, 8000)
	out := m.Mix(voice, nil, DuckGain(-18))
	for i := 0; i < 4; i++ {
		if got := sampleAt(out, i, 0); got != 8000 {
			t.Fatalf("voice was attenuated: %d", got)
		}
	}
}

func TestMusicAloneIsDucked(t *testing.T) {
	m := &Mixer{gain: DuckGain(-6)}
	music := period(4, 10000)
	out := m.Mix(nil, music, DuckGain(-6))
	got := sampleAt(out, 3, 0)
	if got < 4900 || got > 5100 {
		t.Fatalf("expected ~5000 (-6dB of 10000), got %d", got)
	}
}

func TestNothingPlayingReturnsNil(t *testing.T) {
	m := &Mixer{gain: unityGain}
	if out := m.Mix(nil, nil, unityGain); out != nil {
		t.Fatal("with no audio the caller must pump silence, not a buffer")
	}
}

func TestTheRampSettlesEvenWithNoAudio(t *testing.T) {
	// A duck requested while nothing is playing must not sit half-applied
	// waiting for the next period — the next thing to play would fade in
	// from a stale gain.
	m := &Mixer{gain: unityGain}
	target := DuckGain(-18)
	for i := 0; i < 20; i++ {
		m.Mix(nil, nil, target)
	}
	if m.Gain() != target {
		t.Fatalf("ramp did not settle while idle: %d != %d", m.Gain(), target)
	}
}

func TestTheRampReachesItsTargetExactly(t *testing.T) {
	// Integer division floors, so the last steps round to zero and the gain
	// would otherwise stop just short of the target forever.
	m := &Mixer{gain: unityGain}
	target := DuckGain(-18)
	for i := 0; i < 200; i++ {
		m.applyGain(period(2, 1000), target)
	}
	if m.Gain() != target {
		t.Fatalf("gain never reached target: %d != %d", m.Gain(), target)
	}
	// And back up again.
	for i := 0; i < 200; i++ {
		m.applyGain(period(2, 1000), unityGain)
	}
	if m.Gain() != unityGain {
		t.Fatalf("gain never returned to unity: %d", m.Gain())
	}
}

func TestTheRampIsGradualNotAStep(t *testing.T) {
	// A gain change applied at a period boundary is a click, landing on
	// exactly the moment the user is listening to.
	m := &Mixer{gain: unityGain}
	target := DuckGain(-18)
	first := period(64, 10000)
	m.applyGain(first, target)

	start := sampleAt(first, 0, 0)
	end := sampleAt(first, 63, 0)
	if start <= end {
		t.Fatalf("expected a descending ramp across the period, got %d → %d", start, end)
	}
	if start < 9000 {
		t.Fatalf("the ramp should START near the previous gain, got %d", start)
	}
	// It arrives within the period — the duck has to land with the ring —
	// but as a fade: no neighbouring samples may differ by more than a
	// small fraction of the swing, or it is a step with extra steps.
	if m.Gain() != target {
		t.Fatalf("a one-period ramp must arrive in one period, gain %d want %d", m.Gain(), target)
	}
	for i := 1; i < 64; i++ {
		if d := sampleAt(first, i-1, 0) - sampleAt(first, i, 0); d > 10000/32 {
			t.Fatalf("sample %d drops by %d — a step, not a fade", i, d)
		}
	}
}

func TestMixSumsBothStreams(t *testing.T) {
	m := &Mixer{gain: unityGain}
	voice := period(4, 1000)
	music := period(4, 2000)
	out := m.Mix(voice, music, unityGain)
	if got := sampleAt(out, 3, 0); got != 3000 {
		t.Fatalf("expected 1000+2000, got %d", got)
	}
}

func TestMixSaturatesRatherThanWrapping(t *testing.T) {
	// An int16 overflow wraps a loud peak to full-scale opposite polarity,
	// which is a much worse noise than the clipping it replaces.
	m := &Mixer{gain: unityGain}
	voice := period(2, 30000)
	music := period(2, 30000)
	out := m.Mix(voice, music, unityGain)
	if got := sampleAt(out, 0, 0); got != math.MaxInt16 {
		t.Fatalf("expected saturation to +32767, got %d", got)
	}

	m2 := &Mixer{gain: unityGain}
	out2 := m2.Mix(period(2, -30000), period(2, -30000), unityGain)
	if got := sampleAt(out2, 0, 0); got != math.MinInt16 {
		t.Fatalf("expected saturation to -32768, got %d", got)
	}
}

func TestMixHandlesMismatchedLengths(t *testing.T) {
	// The two streams are independent; a short final period on one must not
	// index past the end of the other.
	m := &Mixer{gain: unityGain}
	out := m.Mix(period(8, 100), period(3, 100), unityGain)
	if len(out) != 32 {
		t.Fatalf("output should keep the voice period's length, got %d", len(out))
	}
	if got := sampleAt(out, 0, 0); got != 200 {
		t.Fatalf("overlapping region should be summed, got %d", got)
	}
	if got := sampleAt(out, 7, 0); got != 100 {
		t.Fatalf("the tail beyond the shorter buffer should be untouched, got %d", got)
	}
}

func TestBothChannelsAreScaled(t *testing.T) {
	// L and R are duplicates on this hardware, but the code must not depend
	// on that — the assumption is one refactor away from being wrong.
	m := &Mixer{gain: DuckGain(-6)}
	buf := period(4, 10000)
	m.applyGain(buf, DuckGain(-6))
	l, r := sampleAt(buf, 3, 0), sampleAt(buf, 3, 1)
	if l != r {
		t.Fatalf("channels diverged: L=%d R=%d", l, r)
	}
}
