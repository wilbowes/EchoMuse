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
	// StockCurve runs Radar's own stock FIR EQ (eqFIR), ParametricEQ and
	// OutputTrim ahead of the 8 bands above, additively — see
	// controller/em_eq.py's stock_curve. Has no effect at all on a board
	// without a loaded curve (only Radar, and only when the Echo's own
	// files loaded — RadarTuning). On by default: without it a Radar's
	// speaker has no audible bass (Radar1, 2026-10-10).
	StockCurve bool
	// Jack: a plug is in the jack (speaker.chainForJack). On Radar the
	// stages that exist for its internal speaker — the stock curve and the
	// MBCL bands — are left out; the limiter stays.
	Jack bool
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
		StockCurve:         true,
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

// bassStage is the bass-removal/multiband-compression stage between the EQ
// and the limiter — bassGuard on every board but Radar, radarMultiband on
// it (see newRadarMultiband's own docstring for why Radar's is a real
// 4-band MBCL rather than the single band every other board gets). The
// Process loop and apply() go through this interface so neither needs to
// know which board it is on.
type bassStage interface {
	step(x float64) float64
	reset()
	setEnabled(enabled bool)
	setFloorDb(floorDb float64)
	// takeMaxReductionDb returns the worst reduction since the last call
	// and clears it — read-and-reset in one so TakeStats cannot read a
	// value from one stage and clear a different one.
	takeMaxReductionDb() float64
}

// peakLimiter is the full-band limiter at the end of the chain — limiter on
// every board but Radar, stockLimiter (MBCL's own full-band limiter) on it.
type peakLimiter interface {
	step(x float64) float64
	reset()
	setEnabled(on bool)
	setParams(thresholdDb, releaseMs, fs float64)
	// takeStats returns the worst reduction since the last call (and clears
	// it) and the running clip counts.
	takeStats() (maxReductionDb float64, clipped, clippedBypassed uint64)
}

// Chain runs EQ → bass guard → limiter on stereo S16_LE periods.
//
// Order is em_eq's: the guard removes excursion the driver cannot deliver,
// THEN the limiter catches what is left. Limiting first would spend gain
// reduction on bass about to be thrown away.
//
// The processing is MONO: L and R are averaged, processed once and written
// back to both. The wire is mono and toStereo duplicates it, so the average
// is exact on everything this device plays; stereo output is not supported
// on this hardware (device/CLAUDE.md), and processing two identical channels
// would double the cost for nothing.
//
// Process runs on the ALSA write goroutine only. SetParams and SetActive may
// be called from anywhere; they take effect at the next period.
type Chain struct {
	fs      float64
	boardID string // "biscuit", "radar", or "" — see NewForBoard

	active atomic.Bool // false: Process is a passthrough

	mu      sync.Mutex
	pending *Params // set by SetParams, taken by Process

	// Owned by the ALSA goroutine.
	params Params
	eq     eq
	guard  bassStage
	lim    peakLimiter
	// limFixed: the limiter runs at its own threshold and release (Radar's
	// MBCL full-band limiter), not the ones in Params.
	limFixed                     bool
	skipGuard                    bool // Params.Jack on Radar's multiband
	limThresholdDb, limReleaseMs float64
	idle                         bool // state is all zero and input is silence
	running                      bool // active on the previous period

	// Stock FIR curve (Radar only — nil taps on every other board, and
	// newEQFIR(nil, ...) is nil, so fir stays nil there with no extra
	// gating needed at this level).
	firBands  [][]float64 // the volume-banded stock FIR; nil = unavailable
	firBounds []float64   // each band's upper volume index
	fir       *eqFIR      // lazily sized to the first period's length
	wantFIR   bool        // params.StockCurve as of the last apply()

	// ParametricEQ and OutputTrim: present with the FIR (Radar), run only
	// while it does.
	peq      []biquad
	trimGain float64

	// The volume, applied AHEAD of the chain on Radar (takesVolume), the
	// way stock does it: AudioFlinger attenuates before the AFE's FIR and
	// MBCL see the signal, so MBCL's compressors engage only at a volume
	// that makes them reach their thresholds. Applied after the chain
	// instead — as every board did until this — a 10:1 band-2 compressor
	// sees full-scale audio at every volume and the bass is held down at
	// a level nobody is listening at. preTarget is written by SetVolumeGain
	// from any goroutine; preCur belongs to the ALSA goroutine.
	takesVolume bool
	tookVolume  bool          // the last Process applied the volume; see TookVolume
	preTarget   atomic.Uint64 // math.Float64bits of the gain
	preCur      float64
	inScratch   []float64 // the period's mono input, volume applied
}

// New builds a chain at the given sample rate, inactive, with DefaultParams,
// tuned for biscuit — see NewForBoard for a board-aware chain. Kept so every
// existing caller and test vector (biscuit-only, to date) is unaffected.
func New(sampleRate int) *Chain {
	return NewForBoard(sampleRate, "biscuit", nil)
}

// NewForBoard builds a chain at the given sample rate, inactive, with
// DefaultParams, with the bass guard tuned for boardID (pkg/board.IDOf) —
// see bassGuardTuning. On "radar", rt is the stock tuning read from the Echo
// (LoadRadarTuning): its MBCL replaces the guard and the limiter, and its
// FIR, ParametricEQ and OutputTrim are the stock curve (Params.StockCurve).
// With rt nil, or on any other board, there is no stock curve and the guard
// and limiter are the generic ones. The EQ bands are not board-specific.
func NewForBoard(sampleRate int, boardID string, rt *RadarTuning) *Chain {
	fs := float64(sampleRate)
	radar := boardID == "radar"
	c := &Chain{
		fs:          fs,
		boardID:     boardID,
		eq:          eq{fs: fs},
		guard:       newBassGuard(fs, boardID),
		lim:         newLimiter(fs),
		idle:        true,
		takesVolume: radar,
		preCur:      1,
	}
	c.preTarget.Store(math.Float64bits(1))
	if radar && rt != nil {
		if m := rt.MBCL; m != nil {
			// Radar's real MBCL.cfg is a 4-band multiband compressor, not a
			// copy of biscuit's single band — see newRadarMultiband. Its
			// full-band limiter replaces the generic one, at its own
			// threshold and release whatever a config push carries: there
			// is no dashboard control for either on any board.
			c.guard = newRadarMultiband(fs, m)
			c.lim = newStockLimiter(fs, m.FullBand.LimThresh, m.FullBand.LimRelease, m.FullBand.LimInVol)
			c.limFixed = true
			c.limThresholdDb, c.limReleaseMs = m.FullBand.LimThresh, m.FullBand.LimRelease
		}
		if len(rt.FIRBands) > 0 {
			c.firBands, c.firBounds = rt.FIRBands, rt.FIRBounds
			c.peq = rt.peqBiquads(fs)
			c.trimGain = dbToGain(rt.TrimDb)
		}
	}
	c.apply(DefaultParams())
	return c
}

// Describe is p.String() as this chain runs it. On Radar with its stock
// tuning the limiter is MBCL's own, whatever p says, and the stock curve and
// the jack bypass are named; elsewhere it is p.String() exactly.
func (c *Chain) Describe(p Params) string {
	if c.limFixed {
		p.LimiterThresholdDb, p.LimiterReleaseMs = c.limThresholdDb, c.limReleaseMs
	}
	s := p.String()
	if c.firBands != nil {
		on := "off"
		if p.StockCurve && !p.Jack {
			on = "on"
		}
		s += " stock_curve=" + on
	}
	if p.Jack {
		s += " jack"
	}
	return s
}

// TakesVolume reports whether this chain applies the volume itself, ahead
// of its stages (Radar). When it does and is active, the caller must not
// also apply the volume after it.
func (c *Chain) TakesVolume() bool { return c.takesVolume }

// TookVolume reports whether the last Process applied the volume, so the
// caller applies it after only when the chain did not. Asked of what
// Process DID rather than of Active(), which another goroutine may flip
// between the question and the call. ALSA goroutine only.
func (c *Chain) TookVolume() bool { return c.tookVolume }

// SetVolumeGain sets the linear volume gain the chain ramps to on its next
// period. Ignored by a chain that does not take the volume.
func (c *Chain) SetVolumeGain(g float64) { c.preTarget.Store(math.Float64bits(g)) }

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
	c.guard.setEnabled(p.GuardEnabled)
	c.guard.setFloorDb(math.Min(p.GuardDb, 0))
	c.lim.setEnabled(p.LimiterEnabled)
	limThresholdDb, limReleaseMs := p.LimiterThresholdDb, p.LimiterReleaseMs
	if c.limFixed {
		limThresholdDb, limReleaseMs = c.limThresholdDb, c.limReleaseMs
	}
	c.lim.setParams(limThresholdDb, limReleaseMs, c.fs)
	// Actually turning the FIR on/off is deferred to Process, which is the
	// only place that knows this period's frame count (needed to size it)
	// — apply only records what is WANTED. No effect at all when firBands
	// is nil (every board but Radar).
	c.wantFIR = p.StockCurve && !p.Jack && c.firBands != nil
	_, multiband := c.guard.(*radarMultiband)
	if skip := p.Jack && multiband; skip != c.skipGuard {
		c.guard.reset()
		c.skipGuard = skip
	}
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
	applied, active := c.beginProcess()
	if !active {
		return applied
	}

	frames := len(buf) / 4
	// The period's mono input, with the volume applied first when this
	// chain takes it — same arithmetic as em_eq.StreamingEQ._apply_volume.
	if len(c.inScratch) != frames {
		c.inScratch = make([]float64, frames)
	}
	silentIn, silentOut := true, true
	for i := 0; i < frames; i++ {
		off := i * 4
		l := int16(uint16(buf[off]) | uint16(buf[off+1])<<8)
		r := int16(uint16(buf[off+2]) | uint16(buf[off+3])<<8)
		if l != 0 || r != 0 {
			silentIn = false
		}
		c.inScratch[i] = (float64(l) + float64(r)) / 2
	}
	c.runMono(c.inScratch)

	for i, x := range c.inScratch {
		// Truncation toward zero after runMono's backstop —
		// np.clip(...).astype(int16) in the reference. A chain that takes
		// the volume (Radar) rounds: at the lowest steps (-62dB) the whole
		// signal is a few LSB, and truncating toward zero costs ~6dB of
		// signal-to-error and zeroes anything under 1 LSB. Round half to
		// even, as em_eq._to_int16's np.rint.
		if c.takesVolume {
			x = math.RoundToEven(x)
		}
		s := int16(x)
		if s != 0 {
			silentOut = false
		}
		off := i * 4
		lo, hi := byte(uint16(s)), byte(uint16(s)>>8)
		buf[off], buf[off+1], buf[off+2], buf[off+3] = lo, hi, lo, hi
	}
	c.settleIdle(silentIn, silentOut)
	return applied
}

// ProcessFloat is Process for a wide-precision interleaved stereo period.
// fullScales contains one full-scale multiplier per frame. Samples are
// normalised into the chain's ordinary S16 signal domain and returned to the
// wide domain afterward. Keeping the chain's state normalised makes switching
// between ordinary and boosted periods seamless, while the caller retains
// headroom until the later master-volume stage — unless the chain takes the
// volume (TookVolume), in which case it is already applied.
func (c *Chain) ProcessFloat(buf []float64, fullScales []float64) (applied *Params) {
	applied, active := c.beginProcess()
	if !active {
		return applied
	}

	frames := len(buf) / 2
	if len(c.inScratch) != frames {
		c.inScratch = make([]float64, frames)
	}
	silentIn, silentOut := true, true
	for i := 0; i < frames; i++ {
		l, r := buf[i*2], buf[i*2+1]
		if l != 0 || r != 0 {
			silentIn = false
		}
		c.inScratch[i] = (l + r) / (2 * frameScale(fullScales, i))
	}
	c.runMono(c.inScratch)
	for i, x := range c.inScratch {
		x *= frameScale(fullScales, i)
		if math.Abs(x) >= 1 {
			silentOut = false
		}
		buf[i*2], buf[i*2+1] = x, x
	}
	c.settleIdle(silentIn, silentOut)
	return applied
}

func frameScale(fullScales []float64, i int) float64 {
	if i < len(fullScales) && fullScales[i] > 0 {
		return fullScales[i]
	}
	return 1
}

func (c *Chain) beginProcess() (applied *Params, active bool) {
	if p, changed := c.takePending(); changed {
		applied = &p
	}
	active = c.active.Load()
	if active != c.running {
		// Entering or leaving: the chain's state belongs to audio it last
		// saw, which is not the audio arriving now. Start clean.
		c.reset()
		c.running = active
		// A gain carried from audio the chain last saw would ramp from a
		// stale value; start at where the volume is now.
		c.preCur = math.Float64frombits(c.preTarget.Load())
	}
	c.tookVolume = active && c.takesVolume
	return applied, active
}

// runMono runs the stages over one period of mono samples in the S16
// signal domain, in place, ending at the backstop clip.
func (c *Chain) runMono(x []float64) {
	frames := len(x)
	// FIR on/off is decided in apply(), but SIZED here — this is the first
	// point the chain knows the period's frame count. In production this
	// never changes between calls, so sizing happens once; a device that
	// somehow called Process with a varying frames count would panic
	// inside eqFIR.process, which is the right failure for that bug
	// rather than a wrong answer.
	if c.wantFIR && c.fir == nil {
		c.fir = newEQFIRBands(c.firBands, frames)
		c.fir.startOn(radarEQBand(math.Float64frombits(c.preTarget.Load()), c.firBounds))
	} else if !c.wantFIR && c.fir != nil {
		c.fir = nil
	}
	if c.takesVolume {
		c.applyVolume(x)
	}
	if c.fir != nil {
		// The curve stock plays at this volume, read from the target the
		// volume is ramping to — once per period, as em_eq reads it once
		// per call. A change crossfades across this period.
		c.fir.setBand(radarEQBand(math.Float64frombits(c.preTarget.Load()), c.firBounds))
		copy(x, c.fir.process(x))
	}
	for i, v := range x {
		if c.fir != nil {
			// Radar's stock curve, layered ahead of the bands below —
			// additive with them, same as controller/em_eq.py's
			// StreamingEQ(stock_curve=True), never a replacement.
			for j := range c.peq {
				v = c.peq[j].step(v)
			}
		}
		v = c.eq.step(v)
		if !c.skipGuard {
			v = c.guard.step(v)
		}
		v = c.lim.step(v)
		if c.fir != nil {
			v *= c.trimGain // OutputTrim: after MBCL's limiter, as in AFE.cfg
		}
		// Backstop.
		if v > ceiling {
			v = ceiling
		} else if v < -fullScale {
			v = -fullScale
		}
		x[i] = v
	}
}

// settleIdle: a silent period that came out silent means every filter tail
// has decayed below one LSB. Zero the state and stop processing silence
// until audio returns — otherwise the chain runs flat out on an idle
// speaker, forever. The reset moves the output by less than one LSB.
func (c *Chain) settleIdle(silentIn, silentOut bool) {
	if silentIn && silentOut {
		c.reset()
		c.idle = true
	} else {
		c.idle = false
	}
}

// applyVolume scales x in place, ramping from the gain last applied to the
// target across the period, as speaker.softVolume does: a step in gain
// mid-waveform is a click.
func (c *Chain) applyVolume(x []float64) {
	tgt := math.Float64frombits(c.preTarget.Load())
	cur := c.preCur
	c.preCur = tgt
	if tgt == cur {
		if tgt != 1 {
			for i := range x {
				x[i] *= tgt
			}
		}
		return
	}
	step := (tgt - cur) / float64(len(x))
	g := cur
	for i := range x {
		g += step
		x[i] *= g
	}
}

func (c *Chain) reset() {
	c.eq.reset()
	c.guard.reset()
	c.lim.reset()
	if c.fir != nil {
		c.fir.reset()
	}
	for i := range c.peq {
		c.peq[i].reset()
	}
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
	limRed, clipped, clippedBypassed := c.lim.takeStats()
	s := Stats{
		GuardReductionDb:   c.guard.takeMaxReductionDb(),
		LimiterReductionDb: limRed,
		Clipped:            clipped,
		ClippedBypassed:    clippedBypassed,
	}
	return s
}
