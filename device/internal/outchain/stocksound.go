package outchain

import (
	"math"

	"github.com/wilbowes/EchoMuse/pkg/board"
)

// The stock-sound profile (#229, #247): a driver correction after the user's
// EQ, and three compressor bands above the bass guard, all from the board's
// own Voicing. A port of em_eq.correction_sos and
// em_mbc.BassGuard(upper_bands=True), held to the same vectors as the rest
// of the chain with biscuit's numbers.

// VoiceFilter is em_eq.voice_sos, one cascade per channel. A board with no
// voicing gets an empty one, which passes the voice through.
type VoiceFilter struct{ s [2][]biquad }

func NewVoiceFilter(fs float64, v *board.Voicing) *VoiceFilter {
	f := &VoiceFilter{}
	if v == nil {
		return f
	}
	for ch := range f.s {
		for _, p := range v.Voice {
			f.s[ch] = append(f.s[ch], peaking(p.Hz, p.Db, p.Q, fs))
		}
	}
	return f
}

// Step filters one sample of channel ch (0 or 1).
func (v *VoiceFilter) Step(ch int, x float64) float64 {
	for i := range v.s[ch] {
		x = v.s[ch][i].step(x)
	}
	return x
}

// Reset clears the state, at the start of a voice stream.
func (v *VoiceFilter) Reset() {
	for ch := range v.s {
		for i := range v.s[ch] {
			v.s[ch][i].reset()
		}
	}
}

// correction is the voicing's peaks and shelf, then the notch and an LR4
// high-pass under them (em_eq.correction_sos's order), then the filters whose
// gain follows the volume.
type correction struct {
	s      []biquad
	vol    []biquad
	stages []board.VolumeEQ
	fs     float64
}

func newCorrection(fs float64, v *board.Voicing) correction {
	c := correction{fs: fs}
	if v == nil {
		return c
	}
	for _, p := range v.Correction {
		c.s = append(c.s, peaking(p.Hz, p.Db, p.Q, fs))
	}
	c.s = append(c.s, highShelf(v.Shelf.Hz, v.Shelf.Db, fs),
		peaking(v.Notch.Hz, v.Notch.Db, v.Notch.Q, fs))
	hp := butter2(v.HighpassHz, fs, true)
	c.s = append(c.s, hp, hp)
	c.stages = v.ByVolume
	c.vol = make([]biquad, len(v.ByVolume))
	c.setVolume(100)
	return c
}

// setVolume redesigns the volume filters for a volume in percent, keeping
// their state so the change does not click.
func (c *correction) setVolume(percent float64) {
	for i, st := range c.stages {
		c.vol[i].setCoeffs(peaking(st.Hz, st.GainAt(percent), st.Q, c.fs))
	}
}

func (c *correction) step(x float64) float64 {
	for i := range c.s {
		x = c.s[i].step(x)
	}
	for i := range c.vol {
		x = c.vol[i].step(x)
	}
	return x
}

func (c *correction) reset() {
	for i := range c.s {
		c.s[i].reset()
	}
	for i := range c.vol {
		c.vol[i].reset()
	}
}

// holdMin is the minimum of the last n values pushed, by a monotonic deque:
// em_mbc._BandGain's trailing minimum_filter1d, one sample at a time. Its
// empty state reads as no reduction, as Python's zero-filled tail does.
type holdMin struct {
	n          int
	t          int
	idx        []int
	val        []float64
	head, size int
}

func newHoldMin(ms, fs float64) *holdMin {
	n := max(1, int(math.Round(ms*fs/1000)))
	return &holdMin{n: n, idx: make([]int, n), val: make([]float64, n)}
}

func (h *holdMin) push(v float64) float64 {
	c := len(h.val)
	for h.size > 0 && h.val[(h.head+h.size-1)%c] >= v {
		h.size--
	}
	h.idx[(h.head+h.size)%c], h.val[(h.head+h.size)%c] = h.t, v
	h.size++
	for h.idx[h.head] <= h.t-h.n {
		h.head = (h.head + 1) % c
		h.size--
	}
	h.t++
	return min(h.val[h.head], 0)
}

func (h *holdMin) reset() { h.t, h.head, h.size = 0, 0, 0 }

// allpass2 is the second-order allpass an LR4 crossover's halves sum to
// (em_mbc._allpass2), for a band that skips that crossover.
func allpass2(fc, fs float64) biquad {
	w0 := 2 * math.Pi * fc / fs
	alpha := math.Sin(w0) / (2 * (1 / math.Sqrt(2)))
	a0, a1, a2 := 1+alpha, -2*math.Cos(w0), 1-alpha
	return biquad{b0: a2 / a0, b1: a1 / a0, b2: 1.0, a1: a1 / a0, a2: a2 / a0}
}

type upperBands struct {
	lp2, hp2, lp3, hp3 [2]biquad // LR4 at 500Hz and 7.5kHz
	loAP               [2]biquad // the bass band's allpasses (500Hz, 7.5kHz)
	b2AP               biquad    // the 115-500Hz band's (7.5kHz)
	gainDb             [3]float64
	gain               [3]gainCache
	hold               [3]*holdMin
	smoothDb           [3]float64 // the attack one-pole's output
	// The one-pole's coefficient per band: an instant attack stepped the
	// gain on every new peak, heard as grit on VVV.
	attack         [3]float64
	thresholdLn    [3]float64
	laws           [3]board.BandLaw
	floorDb        float64
	slew           float64
	maxReductionDb float64
}

func newUpperBands(fs float64, v *board.Voicing) *upperBands {
	u := &upperBands{
		slew:    releaseReferenceDb / (math.Max(0.1, v.UpperReleaseMs) / 1000) / fs,
		laws:    v.Upper,
		floorDb: v.UpperFloorDb,
	}
	f2, f3 := v.Crossovers[0], v.Crossovers[1]
	lo2, hi2 := butter2(f2, fs, false), butter2(f2, fs, true)
	lo3, hi3 := butter2(f3, fs, false), butter2(f3, fs, true)
	u.lp2, u.hp2 = [2]biquad{lo2, lo2}, [2]biquad{hi2, hi2}
	u.lp3, u.hp3 = [2]biquad{lo3, lo3}, [2]biquad{hi3, hi3}
	u.loAP = [2]biquad{allpass2(f2, fs), allpass2(f3, fs)}
	u.b2AP = allpass2(f3, fs)
	for i := range u.hold {
		u.hold[i] = newHoldMin(v.UpperHoldMs, fs)
	}
	for i, law := range v.Upper {
		u.attack[i] = 1 - math.Exp(-1/(law.AttackMs*fs/1000))
		u.thresholdLn[i] = fullScale * math.Pow(10, law.ThresholdDb/20)
	}
	return u
}

// bandGain is em_mbc._BandGain for one sample: the static law, held,
// smoothed by the attack one-pole, then instant down and slew-limited up.
func (u *upperBands) bandGain(i int, x float64) float64 {
	target := 0.0
	if a := math.Abs(x); a > u.thresholdLn[i] {
		levelDb := 20 * math.Log10(a/fullScale)
		target = max(-(levelDb-u.laws[i].ThresholdDb)*(1-1/u.laws[i].Ratio), u.floorDb)
	}
	target = u.hold[i].push(target)
	// scipy's lfilter order (b0*x + z, z = -a1*y), each product rounded on
	// its own so no platform fuses them and the result stays bit-exact.
	u.smoothDb[i] = float64(u.attack[i]*target) + float64((1-u.attack[i])*u.smoothDb[i])
	u.gainDb[i] = min(u.smoothDb[i], u.gainDb[i]+u.slew)
	if r := -u.gainDb[i]; r > u.maxReductionDb {
		u.maxReductionDb = r
	}
	return u.gain[i].of(u.gainDb[i])
}

// step takes the guard's two halves and returns the four-band sum. lowGain
// is the guard's gain for the bass band, or nil when the guard is bypassed
// (then no band is compressed, but all are still split and summed).
func (u *upperBands) step(low, high float64, lowGain func(float64) float64) float64 {
	b2 := u.b2AP.step(u.lp2[1].step(u.lp2[0].step(high)))
	rest := u.hp2[1].step(u.hp2[0].step(high))
	b3 := u.lp3[1].step(u.lp3[0].step(rest))
	b4 := u.hp3[1].step(u.hp3[0].step(rest))
	low = u.loAP[1].step(u.loAP[0].step(low))
	if lowGain == nil {
		return low + ((b2 + b3) + b4)
	}
	out := low * lowGain(low)
	out = out + b2*u.bandGain(0, b2)
	out = out + b3*u.bandGain(1, b3)
	out = out + b4*u.bandGain(2, b4)
	return out
}

func (u *upperBands) reset() {
	for _, f := range []*[2]biquad{&u.lp2, &u.hp2, &u.lp3, &u.hp3, &u.loAP} {
		f[0].reset()
		f[1].reset()
	}
	u.b2AP.reset()
	u.gainDb = [3]float64{}
	u.smoothDb = [3]float64{}
	for _, h := range u.hold {
		h.reset()
	}
}
