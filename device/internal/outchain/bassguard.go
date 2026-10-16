package outchain

import "math"

// Bass guard constants, from em_mbc — stock's MBCL.cfg band 1 (#229). See
// that module for why only band 1 exists and why the crossover is LR4.
//
// Biscuit and Radar each have their own measured crossover/threshold: Radar
// runs its own "Radar Tuning V4.5" MBCL.cfg, not a copy of biscuit's. Ratio
// and release are the same on both, so only those two vary — see
// tuningFor.
const (
	crossoverHz     = 115.0
	bassRatio       = 20.0
	bassThresholdDb = -50.0
	bassReleaseMs   = 200.0

	// radarCrossoverHz/radarBassThresholdDb: band 1 of Radar's own MBCL.cfg
	// ("FilterBank FC": [70, 200, 3250], band 1 comp_thresh -25dB), read off
	// a Radar unit's stock firmware. Ratio and release match biscuit's.
	radarCrossoverHz     = 70.0
	radarBassThresholdDb = -25.0

	// releaseReferenceDb: a release time is the time to recover THIS many dB,
	// so the setting means the same thing at 1dB or 12dB of reduction.
	releaseReferenceDb = 10.0

	fullScale = 32768.0
	eps       = 1e-9
)

// bassGuardTuning is the pair of measured values that differ by board.
type bassGuardTuning struct {
	crossoverHz     float64
	bassThresholdDb float64
}

// tuningFor selects the measured tuning for boardID, as reported by
// pkg/board.IDOf. Anything unrecognised — including "unknown" or "" for a
// board this firmware cannot identify — gets biscuit's: the only board this
// was measured against until Radar, and the safer default for a unit this
// package cannot name.
func tuningFor(boardID string) bassGuardTuning {
	if boardID == "radar" {
		return bassGuardTuning{radarCrossoverHz, radarBassThresholdDb}
	}
	return bassGuardTuning{crossoverHz, bassThresholdDb}
}

// bassGuard splits at the board's crossover with a Linkwitz-Riley 4th-order
// pair and applies a 20:1 law from the board's threshold to the low band
// only, floored at `floorDb`.
type bassGuard struct {
	enabled bool
	floorDb float64

	thresholdDb  float64
	thresholdLin float64   // thresholdDb as a sample magnitude
	lp, hp       [2]biquad // LR4 = the Butterworth section applied twice
	slew         float64   // dB per sample the gain may rise
	gainDb       float64
	gain         gainCache

	maxReductionDb float64
}

// newBassGuard builds a guard tuned for boardID — see tuningFor.
func newBassGuard(fs float64, boardID string) *bassGuard {
	t := tuningFor(boardID)
	g := &bassGuard{
		thresholdDb:  t.bassThresholdDb,
		thresholdLin: fullScale * math.Pow(10, t.bassThresholdDb/20),
		slew:         releaseReferenceDb / (math.Max(0.1, bassReleaseMs) / 1000) / fs,
	}
	lo, hi := butter2(t.crossoverHz, fs, false), butter2(t.crossoverHz, fs, true)
	g.lp = [2]biquad{lo, lo}
	g.hp = [2]biquad{hi, hi}
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

	// Below the threshold the target is unity, so the log is skipped: at
	// the threshold that is quiet passages and the gaps between words.
	target := 0.0
	if a := math.Abs(low); a > g.thresholdLin {
		levelDb := 20 * math.Log10(a/fullScale)
		target = max(-(levelDb-g.thresholdDb)*(1-1/bassRatio), g.floorDb)
	}

	// Instant attack, slew-limited release.
	g.gainDb = min(target, g.gainDb+g.slew)
	if r := -g.gainDb; r > g.maxReductionDb {
		g.maxReductionDb = r
	}
	return low*g.gain.of(g.gainDb) + high
}

func (g *bassGuard) reset() {
	for i := range g.lp {
		g.lp[i].reset()
		g.hp[i].reset()
	}
	g.gainDb = 0
}

// setEnabled/setFloorDb/takeMaxReductionDb complete bassStage — see
// chain.go. Radar's radarMultiband implements the same interface, so
// Chain.guard can hold either without the Process loop or apply() caring
// which board it is on.
func (g *bassGuard) setEnabled(enabled bool)    { g.enabled = enabled }
func (g *bassGuard) setFloorDb(floorDb float64) { g.floorDb = floorDb }

func (g *bassGuard) takeMaxReductionDb() float64 {
	r := g.maxReductionDb
	g.maxReductionDb = 0
	return r
}
