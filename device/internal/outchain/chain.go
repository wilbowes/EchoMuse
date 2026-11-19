package outchain

import (
	"fmt"
	"math"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/wilbowes/EchoMuse/pkg/board"
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
	// StockSound adds the board's voicing: the driver correction and the
	// three compressor bands above the guard (#229). It has no effect on a
	// board with no voicing. The device's volume law and order change with
	// it; see speaker.PcmSpeaker.
	StockSound bool
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
		// Always on for the speaker (#229); there is no key to turn it
		// off. chainForJack clears it while a plug is in.
		StockSound: true,
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
	out := fmt.Sprintf("eq=%s speech_boost=%s guard=%s limiter=%s", eqs, boost, guard, lim)
	if p.StockSound {
		out += " stock_sound=on"
	}
	return out
}

// Chain runs EQ → bass guard → limiter on stereo S16_LE periods.
//
// Order is em_eq's: the guard removes excursion the driver cannot deliver,
// THEN the limiter catches what is left. Limiting first would spend gain
// reduction on bass about to be thrown away.
//
// The processing is MONO unless the caller permits stereo: L and R are
// averaged, processed once and written back to both. The controller's wire
// is mono and toStereo duplicates it, so the average is exact on everything
// it sends, and processing two identical channels would double the cost for
// nothing. The Dot 2's internal speaker plays only the RIGHT channel, so
// while it may be heard, L != R must never reach the DAC: the average is
// that guarantee whenever the chain is active.
//
// Stereo (#273, Sendspin with a plug known to be in the jack) is permitted by
// SetStereo, and never with stock sound, whose voicing is for the internal
// driver. Permitted, the chain goes per channel on the first period whose
// channels differ: EQ on each, the bass guard and the limiter LINKED (one
// gain from the louder side on both, so neither moves the image), the right
// channel's state started from the left's, which is what it would have held
// on the dual-mono audio before. On L == R that path computes exactly what
// the mono one does, so the switch is not heard. It goes back to mono on the
// reset after silence, or when the permission is withdrawn, the two sides'
// state then folded to their mean so the output carries on without a step.
// ProcessWide (the stock-sound entry point) is always mono.
//
// INACTIVE, the chain is a passthrough whatever the permission, and L != R
// in is L != R out. The guarantee then rests on the caller: the speaker
// tells the Sendspin player the same thing it tells the chain, and the
// player emits L != R only when told it may.
//
// Process runs on the ALSA write goroutine only. SetParams and SetActive may
// be called from anywhere; they take effect at the next period.
type Chain struct {
	fs float64

	active atomic.Bool // false: Process is a passthrough

	mu      sync.Mutex
	pending *Params // set by SetParams, taken by Process

	// volume is the device level the voicing's volume filters follow, set
	// by SetVolume; volApplied is the one they were last designed for.
	volume     atomic.Int64
	volApplied int64

	// Owned by the ALSA goroutine.
	params  Params
	voicing *board.Voicing // nil: StockSound is never in force
	eq      eq
	guard   *bassGuard
	corr    correction
	lim     *limiter
	idle    bool // state is all zero and input is silence
	running bool // active on the previous period

	// Stereo (#273). stereoOn is the caller's permission, SetStereo's;
	// permit is it as read for this period, and false under stock sound;
	// stereo says the left and right are being processed apart, and eqR is
	// the right channel's EQ while they are (the guard and the limiter hold
	// their own right-channel state).
	stereoOn atomic.Bool
	permit   bool
	stereo   bool
	eqR      eq
}

// New builds a chain at the given sample rate, inactive, with DefaultParams
// and biscuit's bass band and voicing.
func New(sampleRate int) *Chain {
	return NewFor(sampleRate, DefaultGuardBand, board.BiscuitVoicing())
}

// NewFor is New with the board's own bass band and voicing (nil for none).
func NewFor(sampleRate int, band GuardBand, v *board.Voicing) *Chain {
	fs := float64(sampleRate)
	c := &Chain{
		fs:      fs,
		voicing: v,
		eq:      eq{fs: fs},
		eqR:     eq{fs: fs},
		guard:   newBassGuard(fs, band),
		corr:    newCorrection(fs, v),
		lim:     newLimiter(fs),
		idle:    true,
	}
	c.volume.Store(127)
	c.volApplied = 127
	c.apply(DefaultParams())
	return c
}

// SetActive turns processing on or off. Off is a passthrough, which is what
// a device must do while its controller is still processing the audio itself:
// the chain applied twice is a doubled EQ curve and a second limiter.
func (c *Chain) SetActive(on bool) { c.active.Store(on) }

// Active reports whether the chain is processing.
func (c *Chain) Active() bool { return c.active.Load() }

// SetStereo permits (true) or forbids per-channel processing, from the next
// period (#273). The speaker permits it only while a plug is known to be in
// the jack: the internal speaker plays the right channel alone. Forbidden,
// an active chain writes L == R whatever it is given.
func (c *Chain) SetStereo(on bool) { c.stereoOn.Store(on) }

// SetParams queues a new configuration for the next period. Everything is
// updated in place — filter states, the limiter's delay line and its gain
// carry across — so a change mid-song does not click.
func (c *Chain) SetParams(p Params) {
	c.mu.Lock()
	c.pending = &p
	c.mu.Unlock()
}

func (c *Chain) apply(p Params) {
	// Cleared rather than ignored, so everything downstream (the speaker's
	// volume law, the log line) reads what is in force.
	p.StockSound = p.StockSound && c.voicing != nil
	c.params = p
	c.eq.set(p.Bands, p.Loudness)
	c.eqR.set(p.Bands, p.Loudness)
	// The voicing was tuned with the guard on, and drives the chain past
	// full scale near the top, so the profile does not let it be turned off.
	c.guard.enabled = p.GuardEnabled || p.StockSound
	c.guard.floorDb = math.Min(p.GuardDb, 0)
	if p.StockSound {
		c.guard.floorDb = math.Min(c.guard.floorDb, c.voicing.BassFloorDb)
	}
	c.lim.enabled = p.LimiterEnabled
	// The profile's extra filters start cold when it turns on (one phase
	// step at a rare config change), and are dropped when it turns off so
	// the default path stays bit-exact.
	switch {
	case p.StockSound && c.guard.upper == nil:
		c.guard.upper = newUpperBands(c.fs, c.voicing)
		c.guard.hold = newHoldMin(c.voicing.BassHoldMs, c.fs)
		c.corr.reset()
	case !p.StockSound:
		c.guard.upper = nil
		c.guard.hold = nil
	}
	c.lim.setParams(p.LimiterThresholdDb, p.LimiterReleaseMs, c.fs)
}

// SetVolume tells the chain the device level (0..127), for a voicing whose
// EQ follows the volume. It takes effect at the next period.
func (c *Chain) SetVolume(level int) { c.volume.Store(int64(level)) }

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
	prev := c.params
	c.apply(*p)
	changed := c.params != prev
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
	applied, active := c.beginProcess(true)
	if !active {
		return applied
	}
	if c.permit && !c.stereo && !dualMono(buf) {
		c.engage()
	}
	if c.stereo {
		c.processStereo(buf)
		return applied
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
		x := (float64(l) + float64(r)) / 2

		x = c.eq.step(x)
		if c.params.StockSound {
			x = c.corr.step(x)
		}
		x = c.guard.step(x)
		x = c.lim.step(x)

		// Backstop, then truncation toward zero — np.clip(...).astype(int16)
		// in the reference.
		if x > ceiling {
			x = ceiling
		} else if x < -fullScale {
			x = -fullScale
		}
		s := int16(x)
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

// ProcessFloat is Process for a wide-precision interleaved stereo period.
// fullScales contains one full-scale multiplier per frame. Samples are
// normalised into the chain's ordinary S16 signal domain and returned to the
// wide domain afterward. Keeping the chain's state normalised makes switching
// between ordinary and boosted periods seamless, while the caller retains
// headroom until the later master-volume stage.
func (c *Chain) ProcessFloat(buf []float64, fullScales []float64) (applied *Params) {
	applied, active := c.beginProcess(true)
	if !active {
		return applied
	}
	if c.permit && !c.stereo && !dualMonoFloat(buf) {
		c.engage()
	}
	if c.stereo {
		c.processFloatStereo(buf, fullScales)
		return applied
	}

	frames := len(buf) / 2
	silentIn, silentOut := true, true
	for i := 0; i < frames; i++ {
		l, r := buf[i*2], buf[i*2+1]
		if l != 0 || r != 0 {
			silentIn = false
		}
		scale := 1.0
		if i < len(fullScales) && fullScales[i] > 0 {
			scale = fullScales[i]
		}
		x := (l + r) / (2 * scale)
		x = c.eq.step(x)
		if c.params.StockSound {
			x = c.corr.step(x)
		}
		x = c.guard.step(x)
		x = c.lim.step(x)
		if x > ceiling {
			x = ceiling
		} else if x < -fullScale {
			x = -fullScale
		}
		x *= scale
		if math.Abs(x) >= 1 {
			silentOut = false
		}
		buf[i*2], buf[i*2+1] = x, x
	}
	if silentIn && silentOut {
		c.reset()
		c.idle = true
	} else {
		c.idle = false
	}
	return applied
}

// ProcessWide runs the chain over a stereo period at its TRUE level, which
// may exceed full scale, and writes S16 to out. It is the stock-sound
// profile's entry point: the volume is applied BEFORE the chain there, as on
// stock, and near the top it puts the signal over full scale on purpose for the
// compressors and limiter to bring back. Only the chain's output is clipped.
// len(out) must be 2*len(in) bytes (stereo S16 for stereo float).
func (c *Chain) ProcessWide(in []float64, out []byte) (applied *Params) {
	applied, active := c.beginProcess(false)
	frames := min(len(in)/2, len(out)/4)
	silentIn, silentOut := true, true
	for i := 0; i < frames; i++ {
		l, r := in[i*2], in[i*2+1]
		if l != 0 || r != 0 {
			silentIn = false
		}
		x := (l + r) / 2
		if active {
			x = c.eq.step(x)
			if c.params.StockSound {
				x = c.corr.step(x)
			}
			x = c.guard.step(x)
			x = c.lim.step(x)
		}
		if x > ceiling {
			x = ceiling
		} else if x < -fullScale {
			x = -fullScale
		}
		s := int16(x)
		if s != 0 {
			silentOut = false
		}
		lo, hi := byte(uint16(s)), byte(uint16(s)>>8)
		off := i * 4
		out[off], out[off+1], out[off+2], out[off+3] = lo, hi, lo, hi
	}
	if !active {
		return applied
	}
	if silentIn && silentOut {
		c.reset()
		c.idle = true
	} else {
		c.idle = false
	}
	return applied
}

// beginProcess takes what changed since the last period. stereoCapable is
// whether the entry point has a per-channel path (ProcessWide has none).
func (c *Chain) beginProcess(stereoCapable bool) (applied *Params, active bool) {
	if p, changed := c.takePending(); changed {
		applied = &p
	}
	if lv := c.volume.Load(); lv != c.volApplied {
		c.volApplied = lv
		c.corr.setVolume(float64(min(max(lv, 0), 127)) * 100 / 127)
	}
	active = c.active.Load()
	if active != c.running {
		// Entering or leaving: the chain's state belongs to audio it last
		// saw, which is not the audio arriving now. Start clean.
		c.reset()
		c.running = active
	}
	// After takePending, so a period never runs per channel under params
	// that have just turned stock sound on. Read once: the whole period
	// follows one answer.
	c.permit = stereoCapable && c.stereoOn.Load() && !c.params.StockSound
	if c.stereo && !c.permit {
		c.merge()
	}
	return applied, active
}

// engage starts per-channel processing, the right channel's state from the
// left's: on the dual-mono audio until now the two would have been equal.
func (c *Chain) engage() {
	c.eqR = c.eq.clone()
	c.guard.split()
	c.lim.split()
	c.stereo = true
}

// merge ends per-channel processing without a reset, folding each stage's
// two states to their mean, so the mono output that follows carries on from
// where both sides were rather than stepping to one of them.
func (c *Chain) merge() {
	c.eq.merge(&c.eqR)
	c.guard.merge()
	c.lim.merge()
	c.stereo = false
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

// dualMonoFloat is dualMono for an interleaved float period.
func dualMonoFloat(buf []float64) bool {
	for i := 0; i+1 < len(buf); i += 2 {
		if buf[i] != buf[i+1] {
			return false
		}
	}
	return true
}

// processStereo is Process's per-channel path. On L == R it computes what
// the mono body computes, sample for sample; it is a sibling rather than a
// shared body so the mono path's code stays exactly as it was. Stock sound
// is never in force here (beginProcess), so there is no correction stage.
func (c *Chain) processStereo(buf []byte) {
	frames := len(buf) / 4
	silentIn, silentOut := true, true
	for i := 0; i < frames; i++ {
		off := i * 4
		l := int16(uint16(buf[off]) | uint16(buf[off+1])<<8)
		r := int16(uint16(buf[off+2]) | uint16(buf[off+3])<<8)
		if l != 0 || r != 0 {
			silentIn = false
		}
		xl, xr := c.eq.step(float64(l)), c.eqR.step(float64(r))
		xl, xr = c.guard.stepStereo(xl, xr)
		xl, xr = c.lim.stepStereo(xl, xr)

		if xl > ceiling {
			xl = ceiling
		} else if xl < -fullScale {
			xl = -fullScale
		}
		if xr > ceiling {
			xr = ceiling
		} else if xr < -fullScale {
			xr = -fullScale
		}
		sl, sr := int16(xl), int16(xr)
		if sl != 0 || sr != 0 {
			silentOut = false
		}
		buf[off], buf[off+1] = byte(uint16(sl)), byte(uint16(sl)>>8)
		buf[off+2], buf[off+3] = byte(uint16(sr)), byte(uint16(sr)>>8)
	}
	if silentIn && silentOut {
		c.reset()
		c.idle = true
	} else {
		c.idle = false
	}
}

// processFloatStereo is ProcessFloat's per-channel path; see processStereo.
func (c *Chain) processFloatStereo(buf []float64, fullScales []float64) {
	frames := len(buf) / 2
	silentIn, silentOut := true, true
	for i := 0; i < frames; i++ {
		l, r := buf[i*2], buf[i*2+1]
		if l != 0 || r != 0 {
			silentIn = false
		}
		scale := 1.0
		if i < len(fullScales) && fullScales[i] > 0 {
			scale = fullScales[i]
		}
		xl, xr := l/scale, r/scale
		xl, xr = c.eq.step(xl), c.eqR.step(xr)
		xl, xr = c.guard.stepStereo(xl, xr)
		xl, xr = c.lim.stepStereo(xl, xr)
		if xl > ceiling {
			xl = ceiling
		} else if xl < -fullScale {
			xl = -fullScale
		}
		if xr > ceiling {
			xr = ceiling
		} else if xr < -fullScale {
			xr = -fullScale
		}
		xl *= scale
		xr *= scale
		if math.Abs(xl) >= 1 || math.Abs(xr) >= 1 {
			silentOut = false
		}
		buf[i*2], buf[i*2+1] = xl, xr
	}
	if silentIn && silentOut {
		c.reset()
		c.idle = true
	} else {
		c.idle = false
	}
}

func (c *Chain) reset() {
	c.eqR.reset()
	c.stereo = false
	c.eq.reset()
	c.corr.reset()
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
