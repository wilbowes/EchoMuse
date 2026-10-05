package outchain

import "math"

const (
	// ceiling is int16's positive limit. The threshold is measured against it
	// rather than 32768, which would let a 0dBFS peak wrap to full-scale
	// negative on the cast.
	ceiling = 32767.0

	lookaheadMs = 5.0
)

// limiter is em_limiter.Limiter as a per-sample loop: look-ahead peak
// envelope, instant attack, release slewed in dB. The Python vectorises the
// release recursion as a running minimum in sheared coordinates; this is the
// recursion itself, which is what that computes.
//
// The audio is delayed by the look-ahead (lookahead-1 samples), held in a
// delay line primed with silence so every call is 1:1 in length.
type limiter struct {
	enabled                bool
	thresh                 float64 // linear, S16 units
	slew                   float64 // dB per sample the gain may rise
	lookahead              int
	thresholdDb, releaseMs float64

	delay []float64 // lookahead-1 samples, ring
	di    int

	// Sliding maximum of |x| over the look-ahead window: a monotonic deque of
	// (sequence number, value), decreasing in value.
	dqSeq         []int64
	dqVal         []float64
	dqHead, dqLen int
	seq           int64

	gainDb float64
	gain   gainCache

	maxReductionDb           float64
	clipped, clippedBypassed uint64
}

func newLimiter(fs float64) *limiter {
	la := int(fs * lookaheadMs / 1000)
	if la < 1 {
		la = 1
	}
	l := &limiter{
		lookahead: la,
		delay:     make([]float64, la-1),
		dqSeq:     make([]int64, la+1),
		dqVal:     make([]float64, la+1),
	}
	return l
}

func (l *limiter) setParams(thresholdDb, releaseMs, fs float64) {
	l.thresholdDb = math.Min(thresholdDb, 0)
	l.thresh = ceiling * math.Pow(10, l.thresholdDb/20)
	l.releaseMs = releaseMs
	l.slew = releaseReferenceDb / (math.Max(1, releaseMs) / 1000) / fs
}

// ring wraps an index into the deque. A compare, not `%`: 32-bit ARM has no
// divide instruction in Go's build, and the modulo cost a runtime call per
// sample on the device.
func (l *limiter) ring(i int) int {
	if n := len(l.dqSeq); i >= n {
		return i - n
	}
	return i
}

// push adds |x| to the window and drops what has left it.
func (l *limiter) push(a float64) {
	for l.dqLen > 0 {
		if l.dqVal[l.ring(l.dqHead+l.dqLen-1)] > a {
			break
		}
		l.dqLen--
	}
	tail := l.ring(l.dqHead + l.dqLen)
	l.dqSeq[tail], l.dqVal[tail] = l.seq, a
	l.dqLen++
	// The window for the sample leaving the delay line now is the last
	// `lookahead` samples, i.e. sequence numbers > seq-lookahead.
	for l.dqSeq[l.dqHead] <= l.seq-int64(l.lookahead) {
		l.dqHead = l.ring(l.dqHead + 1)
		l.dqLen--
	}
	l.seq++
}

func (l *limiter) step(x float64) float64 {
	l.push(math.Abs(x))

	// The sample whose window just completed.
	var cur float64
	if len(l.delay) > 0 {
		cur = l.delay[l.di]
		l.delay[l.di] = x
		if l.di++; l.di == len(l.delay) {
			l.di = 0
		}
	} else {
		cur = x
	}

	var out float64
	if !l.enabled {
		// Bypassed: unity gain, same delay, and the gain state reads 0dB, as
		// the Python's bypass leaves it.
		l.gainDb = 0
		out = cur
	} else {
		// Under the threshold the target is unity and needs no log — the
		// usual case, since a limiter that is always reducing is set wrong.
		targetDb := 0.0
		if env := l.dqVal[l.dqHead]; env > l.thresh && env > eps {
			targetDb = 20 * math.Log10(max(l.thresh/env, 1e-12))
		}
		l.gainDb = min(targetDb, l.gainDb+l.slew)
		if r := -l.gainDb; r > l.maxReductionDb {
			l.maxReductionDb = r
		}
		out = cur
		if l.gainDb < 0 {
			out = cur * l.gain.of(l.gainDb)
		}
	}

	// #275: a backstop clip while limiting is a bug; while bypassed it is the
	// backstop doing its job on a boosted EQ. Counted apart so the two cannot
	// be confused.
	if math.Abs(out) > ceiling {
		if l.enabled {
			l.clipped++
		} else {
			l.clippedBypassed++
		}
	}
	return out
}

func (l *limiter) reset() {
	for i := range l.delay {
		l.delay[i] = 0
	}
	l.di, l.dqHead, l.dqLen, l.seq = 0, 0, 0, 0
	l.gainDb = 0
}
