package outchain

import "math"

// Bass guard constants, from em_mbc — stock's MBCL.cfg band 1 (#229). See
// that module for why only band 1 exists and why the crossover is LR4.
const (
	crossoverHz     = 115.0
	bassRatio       = 20.0
	bassThresholdDb = -50.0
	bassReleaseMs   = 200.0

	// releaseReferenceDb: a release time is the time to recover THIS many dB,
	// so the setting means the same thing at 1dB or 12dB of reduction.
	releaseReferenceDb = 10.0

	fullScale = 32768.0
	eps       = 1e-9
)

// bassThresholdLin is bassThresholdDb as a sample magnitude.
var bassThresholdLin = fullScale * math.Pow(10, bassThresholdDb/20)

// bassGuard splits at 115Hz with a Linkwitz-Riley 4th-order pair and applies
// a 20:1 law from -50dBFS to the low band only, floored at `floorDb`.
type bassGuard struct {
	enabled bool
	floorDb float64

	lp, hp [2]biquad // LR4 = the Butterworth section applied twice
	// The right channel's crossover, used by stepStereo only. The gain is
	// shared, so a bass note panned to one side cannot move the image.
	lpR, hpR [2]biquad
	slew     float64 // dB per sample the gain may rise
	gainDb   float64
	gain     gainCache

	maxReductionDb float64
}

func newBassGuard(fs float64) *bassGuard {
	g := &bassGuard{
		slew: releaseReferenceDb / (math.Max(0.1, bassReleaseMs) / 1000) / fs,
	}
	lo, hi := butter2(crossoverHz, fs, false), butter2(crossoverHz, fs, true)
	g.lp = [2]biquad{lo, lo}
	g.hp = [2]biquad{hi, hi}
	g.lpR, g.hpR = g.lp, g.hp
	return g
}

func (g *bassGuard) step(x float64) float64 {
	low := g.lp[1].step(g.lp[0].step(x))
	high := g.hp[1].step(g.hp[0].step(x))
	if !g.enabled {
		// Still filtered while bypassed: LR4's halves sum magnitude-flat but
		// not to the identity, so returning x would step the phase at the
		// toggle. The gain state is left where it was, as em_mbc does.
		return low + high
	}

	gl := g.gainFor(math.Abs(low))
	return low*gl + high
}

// stepStereo is step on a stereo frame. The gain follows the louder low band
// and is applied to both, so with L == R it is step exactly, on each side.
func (g *bassGuard) stepStereo(xl, xr float64) (float64, float64) {
	lowL := g.lp[1].step(g.lp[0].step(xl))
	highL := g.hp[1].step(g.hp[0].step(xl))
	lowR := g.lpR[1].step(g.lpR[0].step(xr))
	highR := g.hpR[1].step(g.hpR[0].step(xr))
	if !g.enabled {
		return lowL + highL, lowR + highR
	}
	gl := g.gainFor(max(math.Abs(lowL), math.Abs(lowR)))
	return lowL*gl + highL, lowR*gl + highR
}

// gainFor moves the gain for a low band at magnitude a, and returns it as a
// multiplier.
func (g *bassGuard) gainFor(a float64) float64 {
	// Below the threshold the target is unity, so the log is skipped: at
	// -50dBFS that is quiet passages and the gaps between words.
	target := 0.0
	if a > bassThresholdLin {
		levelDb := 20 * math.Log10(a/fullScale)
		target = max(-(levelDb-bassThresholdDb)*(1-1/bassRatio), g.floorDb)
	}

	// Instant attack, slew-limited release.
	g.gainDb = min(target, g.gainDb+g.slew)
	if r := -g.gainDb; r > g.maxReductionDb {
		g.maxReductionDb = r
	}
	return g.gain.of(g.gainDb)
}

// split starts the right channel's filters from the left's state: on the
// dual-mono audio played until now the two would have been identical.
func (g *bassGuard) split() { g.lpR, g.hpR = g.lp, g.hp }

func (g *bassGuard) reset() {
	for i := range g.lp {
		g.lp[i].reset()
		g.hp[i].reset()
		g.lpR[i].reset()
		g.hpR[i].reset()
	}
	g.gainDb = 0
}
