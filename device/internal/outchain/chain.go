package outchain

import (
	"fmt"
	"math"
	"strings"
	"sync"
	"sync/atomic"
)

// Params is the chain's whole configuration. Defaults match
// em_db.DEFAULT_DEVICE_CONFIG.
type Params struct {
	Bands              [NumBands]float64
	Loudness           bool
	GuardEnabled       bool
	GuardDb            float64
	LimiterEnabled     bool
	LimiterThresholdDb float64
	LimiterReleaseMs   float64
}

// DefaultParams mirrors the controller's defaults, so a device that has not
// yet had a config push sounds the way the controller would have made it.
func DefaultParams() Params {
	return Params{
		GuardEnabled:       true,
		GuardDb:            -30,
		LimiterEnabled:     true,
		LimiterThresholdDb: -1,
		LimiterReleaseMs:   150,
	}
}

// String is the one-line description em_eq.describe_chain gives, so device
// and controller logs read the same way.
func (p Params) String() string {
	eqs := "flat"
	if !isFlat(p.Bands, false) {
		parts := make([]string, NumBands)
		for i, b := range p.Bands {
			if b == 0 {
				parts[i] = "0"
			} else {
				parts[i] = fmt.Sprintf("%+g", b)
			}
		}
		eqs = strings.Join(parts, "/")
	}
	boost := "off"
	if p.Loudness {
		boost = "on"
	}
	guard := "off"
	if p.GuardEnabled {
		guard = fmt.Sprintf("%gdB", math.Min(p.GuardDb, 0))
	}
	lim := "off"
	if p.LimiterEnabled {
		lim = fmt.Sprintf("%gdB/%gms", math.Min(p.LimiterThresholdDb, 0), p.LimiterReleaseMs)
	}
	return fmt.Sprintf("eq=%s speech_boost=%s guard=%s limiter=%s", eqs, boost, guard, lim)
}

// Chain runs EQ → bass guard → limiter on stereo S16_LE periods.
//
// Order is em_eq's: the guard removes excursion the driver cannot deliver,
// THEN the limiter catches what is left. Limiting first would spend gain
// reduction on bass about to be thrown away.
//
// Dual-mono audio — everything the controller sends, since its wire is mono
// and toStereo duplicates it — is processed ONCE: L and R are averaged, which
// is exact when they are equal, and written back to both. Processing two
// identical channels would double the cost for nothing.
//
// Stereo audio (Sendspin with a plug in the jack, #273) is processed per
// channel: EQ on each, and the bass guard and limiter LINKED, one gain from
// the louder side applied to both, so limiting never moves the image. The
// chain switches on the first period whose channels differ, seeding the
// right channel's state from the left's, which is what it would have held on
// the dual-mono audio before; it goes back to mono processing only when it
// resets on silence. With L == R the stereo path computes exactly what the
// mono one does, so the switch itself is inaudible.
//
// Process runs on the ALSA write goroutine only. SetParams and SetActive may
// be called from anywhere; they take effect at the next period.
type Chain struct {
	fs float64

	active atomic.Bool // false: Process is a passthrough

	mu      sync.Mutex
	pending *Params // set by SetParams, taken by Process

	// Owned by the ALSA goroutine.
	params  Params
	eq      eq
	eqR     eq // the right channel's, in stereo
	guard   *bassGuard
	lim     *limiter
	idle    bool // state is all zero and input is silence
	running bool // active on the previous period
	stereo  bool // processing L and R apart, since a period differed
}

// New builds a chain at the given sample rate, inactive, with DefaultParams.
func New(sampleRate int) *Chain {
	fs := float64(sampleRate)
	c := &Chain{
		fs:    fs,
		eq:    eq{fs: fs},
		eqR:   eq{fs: fs},
		guard: newBassGuard(fs),
		lim:   newLimiter(fs),
		idle:  true,
	}
	c.apply(DefaultParams())
	return c
}

// SetActive turns processing on or off. Off is a passthrough, which is what
// a device must do while its controller is still processing the audio itself:
// the chain applied twice is a doubled EQ curve and a second limiter.
func (c *Chain) SetActive(on bool) { c.active.Store(on) }

// Active reports whether the chain is processing.
func (c *Chain) Active() bool { return c.active.Load() }

// SetParams queues a new configuration for the next period. Everything is
// updated in place — filter states, the limiter's delay line and its gain
// carry across — so a change mid-song does not click.
func (c *Chain) SetParams(p Params) {
	c.mu.Lock()
	c.pending = &p
	c.mu.Unlock()
}

func (c *Chain) apply(p Params) {
	c.params = p
	c.eq.set(p.Bands, p.Loudness)
	c.eqR.set(p.Bands, p.Loudness)
	c.guard.enabled = p.GuardEnabled
	c.guard.floorDb = math.Min(p.GuardDb, 0)
	c.lim.enabled = p.LimiterEnabled
	c.lim.setParams(p.LimiterThresholdDb, p.LimiterReleaseMs, c.fs)
}

// takePending applies a queued SetParams. Returns the params that are now in
// force and whether they changed.
func (c *Chain) takePending() (Params, bool) {
	c.mu.Lock()
	p := c.pending
	c.pending = nil
	c.mu.Unlock()
	if p == nil {
		return c.params, false
	}
	changed := *p != c.params
	c.apply(*p)
	return c.params, changed
}

// Idle reports whether processing a silent period would return silence
// unchanged, so the caller can skip it. True while inactive.
func (c *Chain) Idle() bool {
	return !c.active.Load() || c.idle
}

// Process runs the chain over one stereo S16_LE period, IN PLACE, and returns
// the same buffer. The caller must own buf — never pass a shared silence
// buffer.
//
// The first return is non-nil only when a queued SetParams changed the chain
// on this period, so the caller can log what the audio is now going through.
func (c *Chain) Process(buf []byte) (applied *Params) {
	if p, changed := c.takePending(); changed {
		applied = &p
	}
	active := c.active.Load()
	if active != c.running {
		// Entering or leaving: the chain's state belongs to audio it last
		// saw, which is not the audio arriving now. Start clean.
		c.reset()
		c.running = active
	}
	if !active {
		return applied
	}

	if !c.stereo && !dualMono(buf) {
		c.eqR = c.eq.clone()
		c.guard.split()
		c.lim.split()
		c.stereo = true
	}

	frames := len(buf) / 4
	silentIn, silentOut := true, true
	for i := 0; i < frames; i++ {
		off := i * 4
		l := int16(uint16(buf[off]) | uint16(buf[off+1])<<8)
		r := int16(uint16(buf[off+2]) | uint16(buf[off+3])<<8)
		if l != 0 || r != 0 {
			silentIn = false
		}

		if c.stereo {
			xl, xr := c.eq.step(float64(l)), c.eqR.step(float64(r))
			xl, xr = c.guard.stepStereo(xl, xr)
			xl, xr = c.lim.stepStereo(xl, xr)
			sl, sr := toS16(xl), toS16(xr)
			if sl != 0 || sr != 0 {
				silentOut = false
			}
			buf[off], buf[off+1] = byte(uint16(sl)), byte(uint16(sl)>>8)
			buf[off+2], buf[off+3] = byte(uint16(sr)), byte(uint16(sr)>>8)
			continue
		}

		x := (float64(l) + float64(r)) / 2

		x = c.eq.step(x)
		x = c.guard.step(x)
		x = c.lim.step(x)

		s := toS16(x)
		if s != 0 {
			silentOut = false
		}
		lo, hi := byte(uint16(s)), byte(uint16(s)>>8)
		buf[off], buf[off+1], buf[off+2], buf[off+3] = lo, hi, lo, hi
	}

	// A silent period that came out silent means every filter tail has
	// decayed below one LSB. Zero the state and stop processing silence
	// until audio returns — otherwise the chain runs flat out on an idle
	// speaker, forever. The reset moves the output by less than one LSB.
	if silentIn && silentOut {
		c.reset()
		c.idle = true
	} else {
		c.idle = false
	}
	return applied
}

// toS16 is the backstop, then truncation toward zero —
// np.clip(...).astype(int16) in the reference.
func toS16(x float64) int16 {
	if x > ceiling {
		x = ceiling
	} else if x < -fullScale {
		x = -fullScale
	}
	return int16(x)
}

// dualMono reports whether every frame of a stereo S16_LE period has L == R.
func dualMono(buf []byte) bool {
	for off := 0; off+3 < len(buf); off += 4 {
		if buf[off] != buf[off+2] || buf[off+1] != buf[off+3] {
			return false
		}
	}
	return true
}

func (c *Chain) reset() {
	c.eq.reset()
	c.eqR.reset()
	c.stereo = false
	c.guard.reset()
	c.lim.reset()
	c.idle = true
}

// Stats is the chain's instrumentation: the WORK done, as against Params,
// which is what it was set to. A stage that is on and reports 0.00dB never
// engaged, which is a different fault from one that is off.
type Stats struct {
	GuardReductionDb   float64
	LimiterReductionDb float64
	Clipped            uint64 // must stay 0 while limiting
	ClippedBypassed    uint64
}

// TakeStats returns and clears the maximum reductions since the last call.
// ALSA goroutine only.
func (c *Chain) TakeStats() Stats {
	s := Stats{
		GuardReductionDb:   c.guard.maxReductionDb,
		LimiterReductionDb: c.lim.maxReductionDb,
		Clipped:            c.lim.clipped,
		ClippedBypassed:    c.lim.clippedBypassed,
	}
	c.guard.maxReductionDb, c.lim.maxReductionDb = 0, 0
	return s
}
