package outchain

import "math"

// Bass guard law, from stock's MBCL.cfg band 1 (#229); the same on every
// board read so far. See em_mbc for why only band 1 exists and why the
// crossover is LR4.
const (
	bassRatio     = 20.0
	bassReleaseMs = 200.0

	// releaseReferenceDb: a release time is the time to recover THIS many dB,
	// so the setting means the same thing at 1dB or 12dB of reduction.
	releaseReferenceDb = 10.0

	fullScale = 32768.0
	eps       = 1e-9
)

// GuardBand is what the guard protects on a board's driver: the band below
// CrossoverHz, from ThresholdDb. Stock's MBCL.cfg band 1 for that driver.
type GuardBand struct {
	CrossoverHz float64
	ThresholdDb float64
}

// DefaultGuardBand is biscuit's, and the controller's (em_mbc).
var DefaultGuardBand = GuardBand{CrossoverHz: 115, ThresholdDb: -50}

// bassGuard splits at 115Hz with a Linkwitz-Riley 4th-order pair and applies
// a 20:1 law from -50dBFS to the low band only, floored at `floorDb`.
type bassGuard struct {
	enabled bool
	floorDb float64

	thresholdDb  float64
	thresholdLin float64 // thresholdDb as a sample magnitude

	lp, hp [2]biquad // LR4 = the Butterworth section applied twice
	slew   float64   // dB per sample the gain may rise
	gainDb float64
	gain   gainCache

	maxReductionDb float64

	// The stock-sound profile's three bands above the crossover, and its
	// hold on the bass band; nil when the profile is off, which keeps the
	// two-band path bit-exact.
	upper *upperBands
	hold  *holdMin
}

func newBassGuard(fs float64, band GuardBand) *bassGuard {
	g := &bassGuard{
		thresholdDb:  band.ThresholdDb,
		thresholdLin: fullScale * math.Pow(10, band.ThresholdDb/20),
		slew:         releaseReferenceDb / (math.Max(0.1, bassReleaseMs) / 1000) / fs,
	}
	lo, hi := butter2(band.CrossoverHz, fs, false), butter2(band.CrossoverHz, fs, true)
	g.lp = [2]biquad{lo, lo}
	g.hp = [2]biquad{hi, hi}
	return g
}

func (g *bassGuard) step(x float64) float64 {
	low := g.lp[1].step(g.lp[0].step(x))
	high := g.hp[1].step(g.hp[0].step(x))
	if g.upper != nil {
		if !g.enabled {
			return g.upper.step(low, high, nil)
		}
		return g.upper.step(low, high, g.bassGain)
	}
	if !g.enabled {
		// Still filtered while bypassed: LR4's halves sum magnitude-flat but
		// not to the identity, so returning x would step the phase at the
		// toggle. The gain state is left where it was, as em_mbc does.
		return low + high
	}
	return low*g.bassGain(low) + high
}

// bassGain is the guard's law for one sample of the band below the
// crossover: it advances the gain and returns it, linear. Split out so the
// stock-sound profile can apply it to the phase-aligned bass band.
func (g *bassGuard) bassGain(low float64) float64 {
	// Below the threshold the target is unity, so the log is skipped: on
	// biscuit's -50dBFS that is quiet passages and the gaps between words.
	target := 0.0
	if a := math.Abs(low); a > g.thresholdLin {
		levelDb := 20 * math.Log10(a/fullScale)
		target = max(-(levelDb-g.thresholdDb)*(1-1/bassRatio), g.floorDb)
	}
	if g.hold != nil {
		target = g.hold.push(target)
	}

	// Instant attack, slew-limited release.
	g.gainDb = min(target, g.gainDb+g.slew)
	if r := -g.gainDb; r > g.maxReductionDb {
		g.maxReductionDb = r
	}
	return g.gain.of(g.gainDb)
}

func (g *bassGuard) reset() {
	for i := range g.lp {
		g.lp[i].reset()
		g.hp[i].reset()
	}
	g.gainDb = 0
	if g.upper != nil {
		g.upper.reset()
	}
	if g.hold != nil {
		g.hold.reset()
	}
}
