package outchain

// Chain is the whole output path — EQ, bass guard, limiter — plus the thing
// the controller-side reference does not have: PARAMETER CHANGES THAT DO NOT
// CLICK.
//
// That is requirement R4 in docs/audio-states.md section 8, and it is a
// deal-breaker rather than a nicety (Wil, 2026-08-22): device-side shaping
// exists so a change is audible in ~43ms instead of ~4s, and a control that
// clicks every time you touch it is not usable for tuning by ear however fast
// it responds.
//
// WHY THIS IS A LAYER RATHER THAN A CHANGE TO THE STAGES. EQ, BassGuard and
// Limiter are faithful ports, agreeing with the controller's Python bit for
// bit including at parameter changes, and internal/outchain/fixture pins that.
// Smoothing here would break that agreement and remove the only evidence the
// port is correct. So the stages keep reference behaviour, Chain adds the
// divergence, and the divergence gets its own tests.
//
// UPDATING IN PLACE IS NECESSARY AND NOT SUFFICIENT — the mistake worth not
// repeating. Keeping filter state across a coefficient change (which the
// stages do) avoids the transient from REBUILDING a filter. It does nothing
// about the transient from the coefficients themselves changing under a
// running filter. And the obvious fix is a trap: interpolating raw biquad
// coefficients between two stable filters can pass through unstable
// intermediate states, so naive smoothing does not click, it blows up.
//
// The answer is to interpolate the AUDIO, never the coefficients. Two complete
// chains run in parallel — the current one, and a clone carrying the same
// filter state with the new parameters applied — and the output crossfades
// from one to the other. Neither is ever interpolated, so it is
// unconditionally stable whatever the parameters do.

// fadeMS is how long a parameter change takes to complete.
//
// Long enough that no step is audible, short enough that a slider still feels
// direct: at 40ms a change lands within two ALSA periods, so the total
// perceived latency of a tweak stays under ~85ms — still two orders of
// magnitude better than the ~4s the controller-side chain costs.
const fadeMS = 40.0

// Params is the whole chain's settable state — the seven config keys that
// already ride the config push and are, until this lands, ignored by the
// device.
type Params struct {
	Bands              []float64
	Loudness           bool
	LimiterEnabled     bool
	LimiterThresholdDB float64
	LimiterReleaseMS   float64
	GuardEnabled       bool
	GuardDB            float64
}

// Equal reports whether two parameter sets would produce identical audio, so
// a redundant push costs one comparison rather than a crossfade. The
// controller re-sends the whole config on every change and on every reconnect.
func (p Params) Equal(o Params) bool {
	if len(p.Bands) != len(o.Bands) {
		return false
	}
	for i := range p.Bands {
		if p.Bands[i] != o.Bands[i] {
			return false
		}
	}
	return p.Loudness == o.Loudness &&
		p.LimiterEnabled == o.LimiterEnabled &&
		p.LimiterThresholdDB == o.LimiterThresholdDB &&
		p.LimiterReleaseMS == o.LimiterReleaseMS &&
		p.GuardEnabled == o.GuardEnabled &&
		p.GuardDB == o.GuardDB
}

// stages is one complete signal path. Cloning one gives a second path with
// identical state, which is what makes the crossfade seamless: the clone is
// not a cold filter fading in, it is the same filter continuing with different
// coefficients.
type stages struct {
	eq    *EQ
	guard *BassGuard
	lim   *Limiter
}

func newStages(sampleRate int, p Params) *stages {
	return &stages{
		eq:    NewEQ(sampleRate, p.Bands, p.Loudness),
		guard: NewBassGuard(sampleRate, p.GuardDB, p.GuardEnabled),
		lim: NewLimiter(sampleRate, p.LimiterThresholdDB,
			p.LimiterReleaseMS, p.LimiterEnabled),
	}
}

// process runs the chain in its load-bearing order: EQ, then guard, then
// limiter. Limiting first would spend gain reduction on bass that is about to
// be discarded, pulling the midrange down for no reason — measured at 0.5dB on
// a 50Hz + 1kHz mix.
func (s *stages) process(x []float64) []float64 {
	s.eq.Process(x)
	s.guard.Process(x)
	return s.lim.Process(x)
}

// clone copies the path including every piece of carried state, then applies
// the new parameters to the copy.
func (s *stages) clone(p Params) *stages {
	c := &stages{
		eq:    s.eq.clone(),
		guard: s.guard.clone(),
		lim:   s.lim.clone(),
	}
	c.eq.SetBands(p.Bands, p.Loudness)
	c.guard.SetParams(p.GuardDB, p.GuardEnabled)
	c.lim.SetParams(p.LimiterThresholdDB, p.LimiterReleaseMS, p.LimiterEnabled)
	return c
}

// NewChain builds the output chain.
func NewChain(sampleRate int, p Params) *Chain {
	return &Chain{
		fs:       float64(sampleRate),
		fadeLen:  max(1, int(float64(sampleRate)*fadeMS/1000.0)),
		cur:      newStages(sampleRate, p),
		curP:     p,
		rate:     sampleRate,
		fadePos:  -1,
		haveNext: false,
	}
}

// Chain is the output path plus click-free parameter changes.
type Chain struct {
	fs      float64
	rate    int
	fadeLen int

	cur  *stages
	curP Params

	next  *stages
	nextP Params

	// fadePos is -1 when not fading, else how many samples of the fade have
	// been emitted.
	fadePos  int
	haveNext bool

	// pending holds the LATEST target requested while a fade is running.
	// Dragging a slider produces a stream of changes; restarting the fade on
	// each would mean it never completes, and snapping to each would
	// reintroduce exactly the step this exists to remove. So the current fade
	// always runs to completion and the newest request is applied next.
	pending     Params
	havePending bool

	scratch []float64
}

// SetParams requests a parameter change. It never blocks and never touches the
// audio path — the change is applied by the next Process call.
func (c *Chain) SetParams(p Params) {
	if c.haveNext {
		if !p.Equal(c.nextP) {
			c.pending, c.havePending = clonedParams(p), true
		} else {
			c.havePending = false
		}
		return
	}
	if p.Equal(c.curP) {
		return
	}
	c.startFade(p)
}

func clonedParams(p Params) Params {
	q := p
	q.Bands = append([]float64(nil), p.Bands...)
	return q
}

func (c *Chain) startFade(p Params) {
	p = clonedParams(p)
	c.next = c.cur.clone(p)
	c.nextP = p
	c.haveNext = true
	c.fadePos = 0
}

// Fading reports whether a parameter change is currently in flight.
func (c *Chain) Fading() bool { return c.haveNext }

// Process runs one chunk and returns the samples to emit.
//
// The returned slice is valid only until the next call.
func (c *Chain) Process(x []float64) []float64 {
	if len(x) == 0 {
		return nil
	}
	if !c.haveNext {
		return c.cur.process(x)
	}

	// Both paths must see the SAME input, so the old one cannot be given a
	// buffer the new one has already modified in place.
	if cap(c.scratch) < len(x) {
		c.scratch = make([]float64, len(x))
	}
	c.scratch = c.scratch[:len(x)]
	copy(c.scratch, x)

	oldOut := c.cur.process(x)
	newOut := c.next.process(c.scratch)

	n := len(oldOut)
	if len(newOut) < n {
		n = len(newOut)
	}

	// LINEAR CROSSFADE, NOT EQUAL-POWER, AND THAT IS DELIBERATE.
	//
	// Equal-power (cos/sin) is the reflexive choice and is wrong here. It
	// preserves level when the two sources are UNCORRELATED, because their
	// powers add. These two are the same signal through two very similar
	// filters, so they are almost perfectly correlated and their AMPLITUDES
	// add — an equal-power fade would push the sum up by as much as 3dB in
	// the middle, producing an audible bulge on every parameter change. A
	// linear fade is what holds the level constant for correlated sources.
	for i := 0; i < n; i++ {
		t := float64(c.fadePos+i) / float64(c.fadeLen)
		if t > 1 {
			t = 1
		}
		oldOut[i] = oldOut[i]*(1-t) + newOut[i]*t
	}

	c.fadePos += n
	if c.fadePos >= c.fadeLen {
		c.cur, c.curP = c.next, c.nextP
		c.next, c.haveNext = nil, false
		c.fadePos = -1
		if c.havePending {
			p := c.pending
			c.havePending = false
			if !p.Equal(c.curP) {
				c.startFade(p)
			}
		}
	}
	return oldOut[:n]
}

// Flush emits the limiter's held tail at end of stream.
//
// Taken from whichever path is authoritative: mid-fade that is the OUTGOING
// one, since it is the path whose output the listener has mostly been hearing
// and whose tail is contiguous with it. A stream ending inside a 40ms fade is
// a corner, and continuity matters more than which parameters the last 5ms
// used.
func (c *Chain) Flush() []float64 {
	out := c.cur.lim.Flush()
	if c.haveNext {
		c.next.lim.Flush()
	}
	return out
}

// MaxReductionDB reports the worst gain reduction each dynamic stage has
// applied — the instrument for whether a law engaged at all. `0.00` means
// running but idle, which is a completely different state from bypassed and
// indistinguishable from a listening seat.
func (c *Chain) MaxReductionDB() (limiter, guard float64) {
	return c.cur.lim.MaxReductionDB, c.cur.guard.MaxReductionDB()
}

// ─── State cloning ───────────────────────────────────────────────────────────
//
// Each clone must copy carried state, not just configuration. A clone with
// cold filters would fade in a startup transient, which is the artefact this
// whole mechanism exists to avoid.

func (e *EQ) clone() *EQ {
	c := &EQ{fs: e.fs, flat: e.flat}
	c.sections = append([]biquad(nil), e.sections...)
	return c
}

func (g *BassGuard) clone() *BassGuard {
	c := &BassGuard{
		fs:      g.fs,
		enabled: g.enabled,
		lp:      append([]biquad(nil), g.lp...),
		hp:      append([]biquad(nil), g.hp...),
	}
	bg := *g.bass
	c.bass = &bg
	return c
}

func (l *Limiter) clone() *Limiter {
	c := &Limiter{
		fs:             l.fs,
		enabled:        l.enabled,
		thresholdDB:    l.thresholdDB,
		thresh:         l.thresh,
		releaseMS:      l.releaseMS,
		slew:           l.slew,
		lookahead:      l.lookahead,
		gainDB:         l.gainDB,
		MaxReductionDB: l.MaxReductionDB,
		Clipped:        l.Clipped,
	}
	c.tail = append([]float64(nil), l.tail...)
	return c
}
