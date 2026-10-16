package outchain

import "math"

// stockLimiter is stock's MBCL limiter, decoded from Radar's libasp.so —
// the bit-exact mirror of controller/em_limiter.py's StockLimiter, whose
// comment block has the derivation. Radar runs it as MBCL's four band
// limiters and as its full-band limiter.
//
// Look-ahead is fs*0.002 (96 samples) and the attack is retroactive: a
// sample that would exceed the threshold drops the gain to pin it there, and
// the 96 samples already in the delay line are scaled by a ramp from that
// ratio back toward 1. Then a 20-sample hold, then a LINEAR release to unity
// over the configured release clamped to 180..400ms. v = x * g * inVol.
//
// Bypass keeps the delay (the bands must stay aligned and the stream's
// latency must not jump) and returns the gain to unity, as limiter does.
var (
	slLookaheadS = float64(math.Float32frombits(0x3b03126f))
	slMs         = float64(math.Float32frombits(0x3a83126f))
	slHoldFrac   = float64(math.Float32frombits(0x3ed55555))
	slRelMaxS    = float64(math.Float32frombits(0x3ecccccd))
	slInVolMin   = float64(math.Float32frombits(0x3a2566d5))
	slInVolMax   = float64(math.Float32frombits(0x404a62c2))
)

const (
	slRelMinMs = 180.0
	slRelMaxMs = 400.0
)

// stockReleaseSamples is libasp 0x8d75c: the release, clamped, in samples.
func stockReleaseSamples(releaseMs, fs float64) int {
	secs := slRelMaxS
	if releaseMs <= slRelMaxMs {
		secs = math.Max(releaseMs, slRelMinMs) * slMs
	}
	return int(secs * fs)
}

type stockLimiter struct {
	enabled                bool
	la, holdN, relN        int
	c, invRelN             float64
	thresh, inVol          float64
	thresholdDb, releaseMs float64

	ring           []float64
	idx, hold, rel int
	g, stepG, minG float64

	clipped, clippedBypassed uint64
}

func newStockLimiter(fs, thresholdDb, releaseMs, inVolDb float64) *stockLimiter {
	la := int(fs * slLookaheadS)
	l := &stockLimiter{
		enabled: true,
		la:      la,
		holdN:   int(fs * slMs * slHoldFrac),
		c:       1.0 / float64(la),
		inVol:   math.Min(math.Max(math.Pow(10, inVolDb/20), slInVolMin), slInVolMax),
		ring:    make([]float64, la),
		minG:    1,
	}
	l.setParams(thresholdDb, releaseMs, fs)
	l.reset()
	return l
}

func (l *stockLimiter) setParams(thresholdDb, releaseMs, fs float64) {
	l.thresholdDb = math.Min(thresholdDb, 0)
	l.thresh = fullScale * math.Pow(10, l.thresholdDb/20)
	l.releaseMs = releaseMs
	l.relN = stockReleaseSamples(releaseMs, fs)
	l.invRelN = 1.0 / float64(l.relN)
}

func (l *stockLimiter) setEnabled(on bool) { l.enabled = on }

// reset clears the signal state. The reduction and clip counters are
// instrumentation and survive it, as limiter's do.
func (l *stockLimiter) reset() {
	for i := range l.ring {
		l.ring[i] = 0
	}
	l.idx, l.hold, l.rel = 0, 0, 0
	l.g, l.stepG = 1, 0
}

func (l *stockLimiter) step(x float64) float64 {
	var out float64
	if !l.enabled {
		l.g, l.hold, l.rel, l.stepG = 1, 0, 0, 0
		out = l.ring[l.idx]
		l.ring[l.idx] = x * l.inVol
		if math.Abs(out) > ceiling {
			l.clippedBypassed++
		}
	} else {
		v := float64(x*l.g) * l.inVol
		out = l.ring[l.idx]
		l.ring[l.idx] = v
		if peak := math.Abs(v); peak <= l.thresh {
			if l.hold < 1 {
				if l.rel > 0 {
					l.g = l.stepG + l.g
					old := l.rel
					l.rel = old + 1
					if l.relN <= old {
						l.rel, l.g, l.stepG = 0, 1, 0
					}
				}
			} else {
				old := l.hold
				l.hold = old + 1
				if l.holdN <= old {
					l.hold, l.rel = 0, 1
					l.g = l.stepG + l.g
				}
			}
		} else {
			r := l.thresh / peak
			l.g = r * l.g
			f, d, j := r, float64((1-r)*l.c), l.idx
			for n := 0; n < l.la; n++ {
				l.ring[j] = f * l.ring[j]
				f = f + d
				if j < 1 {
					j = l.la
				}
				j--
			}
			l.hold, l.stepG, l.rel = 1, float64((1-l.g)*l.invRelN), 0
		}
		if l.g < l.minG {
			l.minG = l.g
		}
		if math.Abs(out) > ceiling {
			l.clipped++
		}
	}
	if l.idx++; l.idx >= l.la {
		l.idx = 0
	}
	return out
}

func (l *stockLimiter) takeStats() (float64, uint64, uint64) {
	r := -20 * math.Log10(l.minG)
	l.minG = 1
	return r, l.clipped, l.clippedBypassed
}
