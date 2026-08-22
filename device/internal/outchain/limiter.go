package outchain

import "math"

// Look-ahead peak limiter, ported from em_limiter.py.
//
// It stops the EQ clipping what it boosts. The dashboard offers ±12dB faders
// plus a presence boost that stacks on top, and before this existed the chain
// ended in a bare clip — measured at 4.74% of samples clipped at −1dBFS with a
// modest bass boost, 17.95% at the top of the sliders (#231).

const (
	// ceiling is what the threshold is measured against, and it is NOT
	// fullScale. int16 runs −32768..+32767, so a 0dBFS threshold taken
	// against 32768 produces a sample that WRAPS to full-scale negative on
	// the cast — the single worst artefact available, and the one this
	// module exists to prevent.
	ceiling = 32767.0

	// Below this the signal is silence and the gain is left alone. Dividing
	// a threshold by a near-zero envelope gives a huge gain that then has to
	// be clamped, and the clamp is where a click comes from.
	epsGain = 1e-9

	DefaultThresholdDB = -1.0
	DefaultLookaheadMS = 5.0
	DefaultReleaseMS   = 150.0
)

// Limiter is a streaming look-ahead peak limiter. ONE INSTANCE PER STREAM: it
// carries look-ahead and gain state, so two streams through one limiter would
// duck each other — a voice response would pull the gain down on the music
// playing underneath it.
//
// Process takes and returns float samples in S16 units (±32768), matching what
// the EQ works in, so callers do not shuffle scales around.
type Limiter struct {
	fs      float64
	enabled bool

	thresholdDB float64
	thresh      float64
	releaseMS   float64 // kept only so the setting can be reported
	slew        float64 // dB per sample the gain may rise

	lookahead int

	// tail is PRIMED with look-ahead silence so every Process call returns
	// exactly as many samples as it was given. Without the priming the first
	// call comes up short by the look-ahead, which is invisible to a caller
	// that accumulates bytes and reshapes the frames of one that sends back
	// what it gets — em_player does the latter, and its first period arrived
	// 478 bytes short. A limiter must be a drop-in.
	tail   []float64
	gainDB float64

	MaxReductionDB float64
	// Clipped counts emitted samples above the ceiling, faithfully to the
	// reference — including on the BYPASSED path, which is where its
	// documented meaning breaks down.
	//
	// em_limiter's docstring says "if `clipped` is ever non-zero the limiter
	// has a bug". That is true only while it is LIMITING. Bypassed, it is
	// supposed to do nothing, so a boosted EQ ahead of it can and does put
	// samples past the ceiling for the final clip to catch — measured here at
	// 7 samples in `switch_limiter_on` and 86 in `sweep_params`, reproduced
	// bit-for-bit from Python, on ordinary hot material with the limiter off.
	//
	// So read it as "clipped while enabled means a bug"; the counter alone
	// cannot tell you which state produced it. Kept faithful rather than
	// corrected so the two implementations stay comparable — see the note on
	// the port in docs/audio-states.md.
	Clipped int

	// Scratch, so the per-period path allocates nothing.
	buf, env, gain, out []float64
	deq                 []int
}

// NewLimiter builds a limiter. lookahead is not a parameter after
// construction: it sizes the held tail, so changing it mid-stream would drop
// or duplicate the samples sitting in it.
func NewLimiter(sampleRate int, thresholdDB, releaseMS float64, enabled bool) *Limiter {
	l := &Limiter{
		fs:        float64(sampleRate),
		enabled:   enabled,
		lookahead: max(1, int(float64(sampleRate)*DefaultLookaheadMS/1000.0)),
	}
	l.SetParams(thresholdDB, releaseMS, enabled)
	l.tail = make([]float64, max(0, l.lookahead-1))
	return l
}

// SetParams changes the limiter mid-stream without touching carried state.
// The gain state, the tail and the 5ms of latency all persist while disabled,
// so toggling costs no click and no realignment.
func (l *Limiter) SetParams(thresholdDB, releaseMS float64, enabled bool) {
	// Above 0dBFS would ask the limiter to permit clipping, which is the one
	// thing it exists to prevent.
	l.thresholdDB = math.Min(thresholdDB, 0.0)
	l.thresh = ceiling * math.Pow(10.0, l.thresholdDB/20.0)
	l.releaseMS = releaseMS
	l.slew = releaseReferenceDB / (math.Max(1.0, releaseMS) / 1000.0) / l.fs
	l.enabled = enabled
}

// runningMax fills out[i] with the maximum of |x| over [i, i+window).
//
// The tail is padded by the LAST VALUE rather than by zeros: a zero pad would
// let the gain spring back up inside the final samples of a chunk and undo the
// look-ahead exactly where the next chunk's peak is about to arrive.
//
// A monotonic deque rather than the reference's sliding-window view — numpy
// can afford O(n·window) memory, an ALSA period on a 32-bit ARM would rather
// not, and the result is identical because a maximum has no rounding.
func (l *Limiter) runningMax(x, out []float64, window int) {
	n := len(x)
	if window <= 1 {
		for i, v := range x {
			out[i] = math.Abs(v)
		}
		return
	}
	padLen := n + window - 1
	last := 0.0
	if n > 0 {
		last = math.Abs(x[n-1])
	}
	at := func(i int) float64 {
		if i < n {
			return math.Abs(x[i])
		}
		return last
	}

	l.deq = l.deq[:0]
	for i := 0; i < padLen; i++ {
		v := at(i)
		for len(l.deq) > 0 && at(l.deq[len(l.deq)-1]) <= v {
			l.deq = l.deq[:len(l.deq)-1]
		}
		l.deq = append(l.deq, i)
		start := i - window + 1
		if start >= 0 {
			for l.deq[0] < start {
				l.deq = l.deq[1:]
			}
			out[start] = at(l.deq[0])
		}
	}
}

func (l *Limiter) ensure(n int) {
	if cap(l.buf) < n {
		l.buf = make([]float64, n)
		l.env = make([]float64, n)
		l.gain = make([]float64, n)
		l.out = make([]float64, n)
		l.deq = make([]int, 0, n)
	}
	l.buf, l.env, l.gain, l.out = l.buf[:n], l.env[:n], l.gain[:n], l.out[:n]
}

// Process limits one chunk and returns the samples to emit.
//
// The returned slice is scratch owned by the Limiter and is valid only until
// the next call — the caller copies or consumes it immediately. It is the same
// LENGTH as the input in steady state, but the samples are delayed by the
// look-ahead: the chunk is held back and the tail carries the difference.
func (l *Limiter) Process(x []float64) []float64 {
	if len(x) == 0 {
		return nil
	}
	n := len(l.tail) + len(x)
	l.ensure(n)
	copy(l.buf, l.tail)
	copy(l.buf[len(l.tail):], x)

	if !l.enabled {
		// Bypassed: unity gain, but the tail bookkeeping below runs unchanged
		// so the stream keeps its latency and its sample alignment. Dropping
		// the delay instead would shift the audio by 5ms at the moment of the
		// toggle, which is a click.
		for i := range l.gain {
			l.gain[i] = 0
		}
		return l.emit(n)
	}

	l.runningMax(l.buf, l.env, l.lookahead)

	// Shear, take the running minimum, unshear — the same arithmetic as the
	// reference's vectorised form, in the same order, so the two agree to the
	// bit rather than merely closely.
	//
	// THE SEED IS `gainDB + slew`, NOT `gainDB`. The shear is rebased to local
	// index 0, and the release permits one sample of rise between the last
	// emitted sample and this one. Seeding with gainDB alone withholds that
	// step — inaudible on its own at ~0.0014dB, and it makes the streaming
	// path drift from the one-shot path, so the music feed and TTS would stop
	// producing identical audio.
	runMin := math.Inf(1)
	for i := 0; i < n; i++ {
		env := l.env[i]
		target := 1.0
		if env > epsGain {
			target = l.thresh / math.Max(env, epsGain)
		}
		target = math.Min(target, 1.0)
		targetDB := 20.0 * math.Log10(math.Max(target, 1e-12))

		sheared := targetDB - l.slew*float64(i)
		if i == 0 {
			sheared = math.Min(sheared, l.gainDB+l.slew)
		}
		if sheared < runMin {
			runMin = sheared
		}
		g := l.slew*float64(i) + runMin
		l.gain[i] = math.Min(g, 0.0)
	}
	return l.emit(n)
}

// emit applies the gain, holds back the look-ahead, and carries the state.
// Shared by the limiting and bypassed paths so a toggle cannot change the
// stream's framing or latency — only whether the gain is unity.
func (l *Limiter) emit(n int) []float64 {
	for i := 0; i < n; i++ {
		l.out[i] = l.buf[i] * math.Pow(10.0, l.gain[i]/20.0)
	}

	hold := l.lookahead - 1
	var emit []float64
	if hold > 0 {
		emit = l.out[:n-hold]
		// Copy before the next call overwrites buf.
		if cap(l.tail) < hold {
			l.tail = make([]float64, hold)
		}
		l.tail = l.tail[:hold]
		copy(l.tail, l.buf[n-hold:])
		if n > hold {
			l.gainDB = l.gain[n-hold-1]
		}
	} else {
		emit = l.out[:n]
		l.tail = l.tail[:0]
		l.gainDB = l.gain[n-1]
	}

	if len(emit) > 0 {
		worst := 0.0
		for i := 0; i < len(emit); i++ {
			if -l.gain[i] > worst {
				worst = -l.gain[i]
			}
		}
		if worst > l.MaxReductionDB {
			l.MaxReductionDB = worst
		}
		for _, v := range emit {
			if math.Abs(v) > ceiling {
				l.Clipped++
			}
		}
	}
	return emit
}

// Flush emits the held tail at the end of a stream.
//
// Without this the last few milliseconds of every response are dropped —
// inaudible on a long track, and exactly the kind of thing that goes unnoticed
// until someone plays a very short announcement.
func (l *Limiter) Flush() []float64 {
	if len(l.tail) == 0 {
		return nil
	}
	// No further look-ahead is possible, so hold the last gain rather than
	// springing back to unity, which would be an audible step.
	g := math.Pow(10.0, l.gainDB/20.0)
	out := make([]float64, len(l.tail))
	for i, v := range l.tail {
		out[i] = v * g
		if math.Abs(out[i]) > ceiling {
			l.Clipped++
		}
	}
	l.tail = l.tail[:0]
	return out
}
