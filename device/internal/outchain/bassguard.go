package outchain

import "math"

// Bass guard — a dynamic law below 115Hz, ported from em_mbc.py.
//
// The limiter stops the SIGNAL clipping; this stops the DRIVER being asked for
// excursion it does not have. Its parameters are not chosen: they are read off
// stock's `/system/vendor/etc/audio-algorithms/MBCL.cfg` on this exact speaker
// (#229). Band 1 at 20:1 from −50dBFS is not compression, it is dynamic bass
// REMOVAL, and that is the point — removing content the driver cannot produce
// is what stops the cone intermodulating everything above it into mud.
//
// Stock's bands 2–4 are deliberately not implemented here either, for the
// reason em_mbc gives: one gentle 2:1 law at −10dB repeated three times is
// broadband compression for loudness rather than protection, and it would need
// a three-crossover tree with allpass compensation for a benefit nobody has
// heard.

// The law, from stock. Not tuneable.
const (
	CrossoverHz     = 115.0
	bassRatio       = 20.0
	bassThresholdDB = -50.0
	bassReleaseMS   = 200.0

	// DefaultBassGuardDB is how far the bass band may be pulled down. Stock
	// uses −40dB; we default shallower because stock's sits in front of
	// stock's own EQ curve, which we neither have nor have measured.
	DefaultBassGuardDB = -30.0
)

const (
	fullScale = 32768.0
	epsLevel  = 1e-9
)

// releaseReferenceDB is the limiter's shared release constant — both
// processors use the same gain law, so the slew derivation lives in one place.
// See em_limiter.RELEASE_REFERENCE_DB.
const releaseReferenceDB = 10.0

// butter2 returns one second-order Butterworth section, already normalised by
// a0, matching scipy's `butter(2, wn, btype, output="sos")`.
//
// This is the standard bilinear-transform closed form rather than a
// transcription of scipy's zpk pipeline (buttap → lp2lp/lp2hp → bilinear →
// zpk2sos). The two are the same filter and agree to floating-point noise; the
// fixture carries scipy's own coefficients so that agreement is CHECKED rather
// than asserted here.
//
// fc is clamped below Nyquist so an odd sample rate degrades rather than
// producing a filter with no meaning, matching em_mbc._lr4's clamp.
func butter2(fc, fs float64, highpass bool) (b0, b1, b2, a1, a2 float64) {
	wn := math.Min(fc/(fs*0.5), 0.99)
	k := math.Tan(math.Pi * wn / 2)
	k2 := k * k
	norm := 1.0 / (1.0 + math.Sqrt2*k + k2)

	if highpass {
		b0 = norm
		b1 = -2 * norm
		b2 = norm
	} else {
		b0 = k2 * norm
		b1 = 2 * b0
		b2 = b0
	}
	a1 = 2 * (k2 - 1) * norm
	a2 = (1 - math.Sqrt2*k + k2) * norm
	return
}

// lr4 is a Linkwitz-Riley 4th-order half: Butterworth 2nd order applied TWICE.
//
// LR4's defining property is that its lowpass and highpass sum flat (measured
// at 1.7e-11 dB deviation across 4096 points), so with no compression active
// the guard cannot colour anything.
func lr4(fc, fs float64, highpass bool) []biquad {
	b0, b1, b2, a1, a2 := butter2(fc, fs, highpass)
	s := make([]biquad, 2)
	s[0].setCoeffs(b0, b1, b2, a1, a2)
	s[1].setCoeffs(b0, b1, b2, a1, a2)
	return s
}

// bandGain is a band's detector and gain computer: a static curve plus a
// slew-limited release. Kept separate from the filtering, as in the reference,
// because both are easy to get subtly wrong in ways that surface as a pumping
// artefact rather than as an error.
type bandGain struct {
	ratio       float64
	thresholdDB float64
	floorDB     float64
	slew        float64 // dB per sample

	gainDB         float64 // carried across chunks
	maxReductionDB float64
}

func newBandGain(ratio, thresholdDB, releaseMS, floorDB, fs float64) *bandGain {
	return &bandGain{
		ratio:       math.Max(1.0, ratio),
		thresholdDB: thresholdDB,
		floorDB:     math.Min(0.0, floorDB),
		slew:        releaseReferenceDB / (math.Max(0.1, releaseMS) / 1000.0) / fs,
	}
}

// gains writes the per-sample gain in dB (≤ 0) for x into out.
//
// INSTANT ATTACK, SLEW-LIMITED RELEASE. Instant attack is the right choice for
// PROTECTION: the excursion happens on the transient, so a compressor that
// takes 10ms to respond has already let it through.
//
// The reference computes this as a running minimum in a sheared coordinate
// system — vectorised because it is numpy. The recurrence below is what that
// expresses: gain[i] = min(target[i], gain[i-1] + slew), seeded with the
// carried gain PLUS one slew step. That seed is not cosmetic; em_limiter
// records that using the carried gain alone makes the chunked and one-shot
// paths drift apart, and the same law is at work here.
func (g *bandGain) gains(x, out []float64) {
	prev := g.gainDB
	worst := 0.0
	for i, v := range x {
		level := math.Abs(v) / fullScale
		if level < epsLevel {
			level = epsLevel
		}
		levelDB := 20.0 * math.Log10(level)

		// Static curve: above the threshold, keep 1/ratio of the excess.
		over := levelDB - g.thresholdDB
		if over < 0 {
			over = 0
		}
		target := -over * (1.0 - 1.0/g.ratio)
		if target < g.floorDB {
			target = g.floorDB
		}

		gain := prev + g.slew
		if target < gain {
			gain = target
		}
		if gain > 0 {
			gain = 0
		}
		out[i] = gain
		prev = gain
		if -gain > worst {
			worst = -gain
		}
	}
	g.gainDB = prev
	if worst > g.maxReductionDB {
		g.maxReductionDB = worst
	}
}

// BassGuard is a streaming two-band compressor: a hard dynamic law below the
// crossover, unity above it.
//
// One instance per audio stream — it carries filter and gain state, so sharing
// one would let a voice response compress the music underneath it.
type BassGuard struct {
	fs      float64
	enabled bool

	lp, hp []biquad
	bass   *bandGain

	// Scratch, so a per-period call allocates nothing on the ALSA path.
	low, high, gain []float64
}

// NewBassGuard builds a guard at the fixed crossover. The crossover is
// deliberately not a parameter: it owns filter state, so moving it mid-stream
// would mean rebuilding the biquads and either carrying incompatible state or
// zeroing it — an audible thump at exactly the moment someone is listening for
// a difference. It is a measured value off the hardware, not a taste one.
func NewBassGuard(sampleRate int, bassGuardDB float64, enabled bool) *BassGuard {
	fs := float64(sampleRate)
	return &BassGuard{
		fs:      fs,
		enabled: enabled,
		lp:      lr4(CrossoverHz, fs, false),
		hp:      lr4(CrossoverHz, fs, true),
		bass: newBandGain(bassRatio, bassThresholdDB, bassReleaseMS,
			math.Min(0.0, bassGuardDB), fs),
	}
}

// SetParams changes the guard mid-stream without touching carried state.
// Depth is the parameter that wants tuning by ear in a real room.
func (g *BassGuard) SetParams(bassGuardDB float64, enabled bool) {
	g.bass.floorDB = math.Min(0.0, bassGuardDB)
	g.enabled = enabled
}

// MaxReductionDB is the worst reduction so far — the instrument for whether
// this is doing anything, and for whether it is doing too much. `0.00` means
// running but idle, which is a completely different state from bypassed and
// indistinguishable from a listening seat.
func (g *BassGuard) MaxReductionDB() float64 { return g.bass.maxReductionDB }

func (g *BassGuard) ensure(n int) {
	if cap(g.low) < n {
		g.low = make([]float64, n)
		g.high = make([]float64, n)
		g.gain = make([]float64, n)
	}
	g.low, g.high, g.gain = g.low[:n], g.high[:n], g.gain[:n]
}

// Process compresses one chunk in place.
//
// BYPASSED STILL FILTERS, AND MUST NOT BE SIMPLIFIED TO A NO-OP. LR4's two
// halves sum magnitude-flat, but the sum is an ALLPASS, not the identity — so
// returning x unchanged would step the phase at the toggle, and skipping the
// filters entirely would leave them cold to ring on re-enable. Both are
// audible as a thump at exactly the moment someone is listening for the
// difference the toggle made. The signal takes the same path in both states;
// only the gain law changes.
func (g *BassGuard) Process(x []float64) {
	if len(x) == 0 {
		return
	}
	g.ensure(len(x))
	copy(g.low, x)
	copy(g.high, x)
	for i := range g.lp {
		g.lp[i].process(g.low)
	}
	for i := range g.hp {
		g.hp[i].process(g.high)
	}

	if !g.enabled {
		for i := range x {
			x[i] = g.low[i] + g.high[i]
		}
		return
	}

	g.bass.gains(g.low, g.gain)
	for i := range x {
		x[i] = g.low[i]*math.Pow(10.0, g.gain[i]/20.0) + g.high[i]
	}
}
