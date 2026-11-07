package echoref

import (
	"encoding/binary"
	"math"
	"math/rand"
	"testing"
)

// speechish is band-limited noise with a syllable-rate envelope: enough
// structure for one clear correlation peak, like speech.
func speechish(n int, seed int64) []float64 {
	r := rand.New(rand.NewSource(seed))
	out := make([]float64, n)
	var lp float64
	for i := range out {
		lp = 0.7*lp + 0.3*r.NormFloat64()
		env := 0.5 + 0.5*math.Sin(2*math.Pi*4*float64(i)/48000)
		out[i] = 6000 * lp * env
	}
	return out
}

// loopbackBytes renders 48 kHz mono as interleaved stereo S16LE.
func loopbackBytes(x []float64) []byte {
	b := make([]byte, 4*len(x))
	for i, v := range x {
		s := uint16(int16(v))
		binary.LittleEndian.PutUint16(b[4*i:], s)
		binary.LittleEndian.PutUint16(b[4*i+2:], s)
	}
	return b
}

// rig plays src through a pretend room: the microphones hear the 16 kHz
// decimation of src, scaled, delayed by delay16 samples behind the
// reference's own count, plus a little noise.
type rig struct {
	a        *Aligner
	src      []float64 // 48 kHz
	ref16    []int16   // what the aligner's ring will hold, by index
	delay16  int
	micIndex int64
	refPos   int // 48 kHz samples pushed
}

func newRig(seconds int, delay16 int) *rig {
	src := speechish(seconds*48000, 1)
	a := New()
	// The aligner's own decimation of src, for building the microphone side.
	d := newDecimator()
	ref16 := d.run(loopbackBytes(src))
	return &rig{a: a, src: src, ref16: ref16, delay16: delay16}
}

// step pushes 160 ms of loopback (optionally slipping by repeating a 16 ms
// period) and takes 160 ms of microphone audio; returns Take's result.
func (r *rig) step(slipRepeat bool) []byte { return r.stepN(slipRepeat, 1) }

// stepN is step with a repeat of n periods when slipping.
func (r *rig) stepN(slipRepeat bool, n int) []byte {
	chunk := 7680
	if r.refPos+chunk > len(r.src) {
		return nil
	}
	if slipRepeat {
		// The driver repeats one 768-frame period: the reference count runs
		// 256 samples ahead of the audio from here on. The room is unchanged.
		for i := 0; i < n; i++ {
			r.a.PushLoopback(loopbackBytes(r.src[r.refPos : r.refPos+768]))
		}
	}
	r.a.PushLoopback(loopbackBytes(r.src[r.refPos : r.refPos+chunk]))
	r.refPos += chunk
	mic := make([]int16, 2560)
	rnd := rand.New(rand.NewSource(r.micIndex))
	for i := range mic {
		// The echo the microphone hears at index m is reference audio from
		// index m - delayBehind, where the reference count runs delay16 ahead.
		k := int(r.micIndex) + i + r.delay16 - 3*256 - 120
		var v float64
		if k >= 0 && k < len(r.ref16) {
			v = 0.5 * float64(r.ref16[k])
		}
		mic[i] = int16(v + 30*rnd.NormFloat64())
	}
	r.a.Observe(r.micIndex, mic)
	out := r.a.Take(r.micIndex, len(mic))
	r.micIndex += 2560
	return out
}

func TestLocksThenReturnsTheEchoesOwnReferenceLeadingByLead(t *testing.T) {
	r := newRig(12, 0)
	var got []byte
	for i := 0; i < 50; i++ {
		got = r.step(false)
	}
	if !r.a.Locked() || got == nil {
		t.Fatalf("not locked after 8 s of audio: searches=%d misses=%d head=%d", r.a.Searches, r.a.Misses, r.a.head)
	}
	// The echo at mic index m came from ref16[m - 888]. A reference that
	// leads must hand out, for mic sample m, the audio the mic hears at
	// m+Lead: ref16[m - 888 + Lead].
	want := int64(-3*256-120) + Lead
	if r.a.offset != want {
		t.Errorf("offset %d, want %d", r.a.offset, want)
	}
	// And directly: the returned reference, shifted Lead later, is the echo.
	r.a.PushLoopback(loopbackBytes(r.src[r.refPos : r.refPos+7680]))
	r.refPos += 7680
	mic := make([]int16, 2560)
	for i := range mic {
		mic[i] = int16(0.5 * float64(r.ref16[int(r.micIndex)+i-888]))
	}
	r.a.Observe(r.micIndex, mic)
	ref := r.a.Take(r.micIndex, len(mic))
	if ref == nil {
		t.Fatal("no reference with the loopback already pushed")
	}
	for i := Lead; i < len(mic); i++ {
		got := int16(binary.LittleEndian.Uint16(ref[2*(i-Lead):]))
		if want := r.ref16[int(r.micIndex)+i-888]; got != want {
			t.Fatalf("sample %d: reference %d, echo source %d: the reference does not lead by Lead", i, got, want)
		}
	}
}

func TestASlippedPeriodIsFollowedAfterTwoAgreeingSearches(t *testing.T) {
	r := newRig(20, 0)
	for i := 0; i < 40; i++ {
		r.step(false)
	}
	before := r.a.offset
	r.step(true)
	for i := 0; i < 40; i++ {
		r.step(false)
	}
	if got := r.a.offset - before; got != 256 {
		t.Errorf("offset moved %d, want 256 (one 16 ms period); moves=%d", got, r.a.Moves)
	}
}

func TestNothingIsReturnedOverSilenceOrBeforeALock(t *testing.T) {
	a := New()
	silent := make([]byte, 4*7680)
	mic := make([]int16, 2560)
	var idx int64
	for i := 0; i < 30; i++ {
		a.PushLoopback(silent)
		a.Observe(idx, mic)
		if got := a.Take(idx, len(mic)); got != nil {
			t.Fatal("a reference with nothing to align it by")
		}
		idx += 2560
	}
	if a.Locked() || a.Searches != 0 {
		t.Errorf("locked=%v searches=%d over silence", a.Locked(), a.Searches)
	}
}

// The low-pass keeps the speech band and removes what would fold: a 12 kHz
// tone at 48 kHz aliases to 4 kHz if kept every third sample.
func TestTheDecimatorRemovesWhatWouldAlias(t *testing.T) {
	tone := func(f float64) float64 {
		x := make([]float64, 48000)
		for i := range x {
			x[i] = 10000 * math.Sin(2*math.Pi*f*float64(i)/48000)
		}
		d := newDecimator()
		y := d.run(loopbackBytes(x))
		return rms(y[200:])
	}
	if pass := tone(1000); pass < 6500 || pass > 7600 {
		t.Errorf("1 kHz rms %.0f, want ~7071", pass)
	}
	if stop := tone(12000); stop > 200 {
		t.Errorf("12 kHz leaks %.0f rms", stop)
	}
}

// The old whole-ring reader repeated 144 ms at a time, past the locked
// search: the lock must be dropped (nothing returned meanwhile) and found
// again at the new offset.
func TestAJumpPastTheLockedSearchIsLostThenFoundAgain(t *testing.T) {
	r := newRig(30, 0)
	for i := 0; i < 40; i++ {
		r.step(false)
	}
	before := r.a.offset
	r.stepN(true, 9) // 9 periods = 2304 samples = 144 ms
	sawNil := false
	for i := 0; i < 80; i++ {
		if r.step(false) == nil {
			sawNil = true
		}
	}
	if r.a.Losses == 0 || !sawNil {
		t.Errorf("losses=%d sawNil=%v: a stale lock was kept", r.a.Losses, sawNil)
	}
	if !r.a.Locked() || r.a.offset-before != 2304 {
		t.Errorf("locked=%v moved %d, want 2304", r.a.Locked(), r.a.offset-before)
	}
}
