package outchain

import "math"

// stockComp is stock's MBCL band compressor, decoded from Radar's libasp.so
// — the bit-exact mirror of controller/em_mbc.py's StockCompressor, whose
// docstring has the derivation. Per 1ms block: power of y = x*inVol, a gate
// on a fast smoother against a slow floor, a level smoothed toward the
// power (attack ~43ms, release ~435ms), a gain computer, a gain smoothed
// again (~654ms), applied to y delayed 768 samples (16ms look-ahead).
//
// Streaming holds one extra block (48 samples) because a block's gain needs
// the whole block: step returns the output of the last COMPLETED block, so
// the delay is 768+48 on every band alike and their sum stays aligned.
//
// Products that feed a sum are wrapped in float64() so no platform fuses
// them into an FMA the Python cannot reproduce.
const stockCompDelay = 768

var (
	compGateFast   = float64(math.Float32frombits(0x3c2aaaab))
	compFloorUp    = float64(math.Float32frombits(0x382ec33e))
	compFloorDown  = float64(math.Float32frombits(0x39da740e))
	compGateRatio  = float64(math.Float32frombits(0x3916feb5))
	compAttack     = float64(math.Float32frombits(0x3cbbbbbc))
	compRelease    = float64(math.Float32frombits(0x3b162fc9))
	compGainSmooth = float64(math.Float32frombits(0x3ac83fb7))
	compInit       = float64(math.Float32frombits(0x3c23d70a))
	compInVolMin   = float64(math.Float32frombits(0x3dcccccd))
	compInVolMax   = float64(math.Float32frombits(0x40b3f300))
)

type stockComp struct {
	enabled                      bool
	block                        int
	inVol, powScale, kHalf, tpow float64
	floorDb, floor, thresholdDb  float64
	a, b, level, gain, minGain   float64
	ring                         [stockCompDelay]float64
	ri                           int
	in, out                      []float64
	p                            int
}

func newStockComp(fs, ratio, thresholdDb, floorDb, inVolDb float64) *stockComp {
	block := int(fs) / 1000
	ratio = math.Min(math.Max(ratio, 1), 20)
	c := &stockComp{
		enabled:     true,
		block:       block,
		inVol:       math.Min(math.Max(math.Pow(10, inVolDb/20), compInVolMin), compInVolMax),
		powScale:    1.0 / (float64(block) * fullScale * fullScale),
		kHalf:       (1.0 - 1.0/ratio) * 0.5,
		tpow:        math.Pow(10, math.Min(math.Max(thresholdDb, -90), 0)/10),
		in:          make([]float64, block),
		out:         make([]float64, block),
		minGain:     1,
		thresholdDb: thresholdDb,
	}
	c.setFloorDb(floorDb)
	c.reset()
	return c
}

func (c *stockComp) setFloorDb(floorDb float64) {
	c.floorDb = math.Min(math.Max(floorDb, -40), 0)
	c.floor = math.Pow(10, c.floorDb/20)
}

func (c *stockComp) reset() {
	c.a, c.b, c.level, c.gain = compInit, compInit, compInit, 1
	for i := range c.ring {
		c.ring[i] = 0
	}
	for i := range c.out {
		c.out[i] = 0
	}
	c.ri, c.p = 0, 0
}

func (c *stockComp) step(x float64) float64 {
	o := c.out[c.p]
	c.in[c.p] = x
	if c.p++; c.p == c.block {
		c.runBlock()
		c.p = 0
	}
	return o
}

func (c *stockComp) runBlock() {
	var sum float64
	for i, x := range c.in {
		y := x * c.inVol
		c.in[i] = y
		sum = sum + float64(y*y)
	}
	for i, y := range c.in {
		c.out[i] = c.ring[c.ri+i]
		c.ring[c.ri+i] = y
	}
	if c.ri += c.block; c.ri >= stockCompDelay {
		c.ri = 0
	}
	if !c.enabled {
		return
	}
	p := sum * c.powScale
	a := c.a + float64(compGateFast*(p-c.a))
	b := c.b
	coef := compFloorUp
	if a < b {
		coef = compFloorDown
	}
	b = b + float64(coef*(a-b))
	c.a, c.b = a, b
	lvl := c.level
	if a > b*compGateRatio {
		coef = compRelease
		if lvl < p {
			coef = compAttack
		}
		lvl = lvl + float64(coef*(p-lvl))
		c.level = lvl
	}
	g := 1.0
	if lvl > c.tpow {
		if g = math.Pow(c.tpow/lvl, c.kHalf); g <= c.floor {
			g = c.floor
		}
	}
	gain := c.gain + float64(compGainSmooth*(g-c.gain))
	c.gain = gain
	if gain < c.minGain {
		c.minGain = gain
	}
	for i := range c.out {
		c.out[i] = gain * c.out[i]
	}
}

// takeMaxReductionDb returns the deepest gain reduction since the last call
// and clears it.
func (c *stockComp) takeMaxReductionDb() float64 {
	r := -20 * math.Log10(c.minGain)
	c.minGain = 1
	return r
}
