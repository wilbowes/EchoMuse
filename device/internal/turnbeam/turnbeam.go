// Package turnbeam is the wake-anchored turn stage: once the wake word has
// fired, it beams the turn toward the talker and says when the talker has
// stopped, using only the room's own spatial statistics.
//
// Both halves read one set of per-bin spatial covariances of the cancelled
// microphones, kept as 128ms block sums over the last few seconds:
//
//   - Noise: the blocks before the wake word. In a household with the TV on
//     that is the TV, from its fixed speakers, as the array hears it.
//   - Talker: the blocks holding the wake word itself (WakeS before Lock).
//
// Beam: MVDR toward the talker against that noise. The steering vector is
// the principal generalised eigenvector of (talker, noise), mapped back from
// the whitened space and made distortionless on the talker's best mic, with
// diagonal loading. The filter is fixed for the turn.
//
// Endpoint: whiten each 256ms window by the pre-wake noise and measure the
// share of power arriving along the talker's whitened steering vector
// (Souden et al. 2010's multichannel speech presence, as a scalar). The
// talker raises it above its recent floor (minimum statistics); the turn ends
// HangS after the last window presenceRatio above that floor. The same
// decision gates the output (delayed GateDelay so it lands on the audio it
// describes): TV-only stretches inside the turn go down 26 dB.
//
// Measured offline on donut 41C8, lounge, TV at 12 o'clock with speakers at 3
// and 9 (2026-11-21, docs/history/device.md): beam +4.1 to +5.4 dB talker
// over TV, and the endpoint gave HA's STT exactly the command at 2.4-2.6 s,
// for a talker at 6 and 4 o'clock. In line with a TV speaker (8 o'clock,
// ~4 m) neither half separates: a 7cm array has no spatial resolution there.
// Nothing about the talker's voice is modelled.
package turnbeam

import (
	"errors"
	"math"
	"math/cmplx"
	"sort"
)

const (
	sampleRate  = 16000
	frameN      = 512 // STFT frame, sqrt-Hann, 50% overlap: perfect reconstruction
	hopH        = frameN / 2
	bins        = frameN/2 + 1
	blockFrames = 8 // 8 hops = 128ms per covariance block
	blockS      = float64(blockFrames*hopH) / sampleRate
	bandLoHz    = 300
	bandHiHz    = 4000
	// The talker is present while the score is presenceRatio above its
	// floor over the last floorS. Set on six lounge TV takes: the command
	// peaked 1.3-1.9x its floor, TV-only bumps after it at most 1.2x.
	floorS        = 2.0
	presenceRatio = 1.3
	// Gate: the beam's output is delayed GateDelay so each presence decision
	// (a 256ms window ending now) lands on the audio it describes, then held
	// at full level while the talker is present and gateRelease after, and
	// at gateFloor otherwise. TV words between the wake and the command, and
	// after it, reached HA through the beam (5 dB down is still words):
	// "I was in a good place. Tell me a long story." (41C8 live 2026-11-21).
	GateDelay   = 2 * blockFrames * hopH // samples (256ms)
	gateRelease = 0.3
	gateFloor   = 0.05 // -26 dB
)

// Config sets the stage's windows. Zero fields take the defaults.
type Config struct {
	Mics   int     // cancelled microphone channels, in the board's order
	NoiseS float64 // pre-wake noise window (default 6.5s)
	WakeS  float64 // window before Lock that holds the wake word (default 1.6s)
	Load   float64 // diagonal loading, share of the mean noise power (default 1e-3)
	HangS  float64 // talker gone this long after a crossing ends the turn (default 0.75s)
	StartS float64 // no crossing at all this long after Lock ends the turn (default 3s)
}

func (c *Config) defaults() {
	if c.NoiseS <= 0 {
		c.NoiseS = 6.5
	}
	if c.WakeS <= 0 {
		c.WakeS = 1.6
	}
	if c.Load <= 0 {
		c.Load = 1e-3
	}
	if c.HangS <= 0 {
		c.HangS = 0.75
	}
	if c.StartS <= 0 {
		c.StartS = 3
	}
}

// Stage holds the STFT state, the covariance blocks and, once locked, the
// beam and the endpoint detector. Not safe for concurrent use: one capture
// goroutine owns it.
type Stage struct {
	cfg  Config
	m    int
	fft  *fft
	win  []float64
	tail [][]float64 // per mic, the last hopH input samples
	pend [][]float64 // per mic, samples waiting for a full hop

	// Covariance blocks: ring of block sums (complex64, bins×m×m each).
	ring   [][]complex64
	head   int // next slot
	filled int
	cur    []complex64
	curN   int

	// Scratch per frame.
	re, im [][]float64
	xb     [][]complex128 // [bin][mic]

	locked bool
	h      [][]complex128 // [bin][mic] beam weights
	ref    int
	ola    []float64
	// Detector.
	whL     [][]complex128 // [bin] Cholesky of the loaded noise covariance
	whU     [][]complex128 // [bin] whitened unit steering
	thr     float64
	score   float64
	sinceS  float64 // since Lock
	lastHit float64 // time (since Lock) of the last window over thr; <0 none
	ended   bool
	wakeScore float64 // the detector over the wake word's own blocks
	preP99    float64 // the 99th percentile of the pre-wake windows (observability)
	floorHist []float64 // scores the floor is taken over: the pre-wake TV, then the turn
	dly       []float64 // gate delay line (GateDelay samples)
	hold      float64   // seconds of full level left
	gain      float64   // smoothed gate gain
	Trace     []float64 // per-block scores since Lock (observability; bounded by the turn)
}

// New returns an unlocked stage.
func New(cfg Config) *Stage {
	cfg.defaults()
	m := cfg.Mics
	s := &Stage{cfg: cfg, m: m, fft: newFFT(frameN), win: make([]float64, frameN)}
	for i := range s.win {
		s.win[i] = math.Sqrt(0.5 - 0.5*math.Cos(2*math.Pi*float64(i)/frameN))
	}
	nBlocks := int(math.Ceil((cfg.NoiseS+cfg.WakeS)/blockS)) + 2
	s.ring = make([][]complex64, nBlocks)
	for i := range s.ring {
		s.ring[i] = make([]complex64, bins*m*m)
	}
	s.cur = make([]complex64, bins*m*m)
	s.tail = make([][]float64, m)
	s.pend = make([][]float64, m)
	s.re = make([][]float64, m)
	s.im = make([][]float64, m)
	for i := 0; i < m; i++ {
		s.tail[i] = make([]float64, hopH)
		s.re[i] = make([]float64, frameN)
		s.im[i] = make([]float64, frameN)
	}
	s.xb = make([][]complex128, bins)
	for k := range s.xb {
		s.xb[k] = make([]complex128, m)
	}
	s.ola = make([]float64, frameN)
	return s
}

// Locked, Ended, Score, Threshold and RefMic are the stage's observables.
func (s *Stage) Locked() bool       { return s.locked }
func (s *Stage) Ended() bool        { return s.ended }
func (s *Stage) Score() float64     { return s.score }
func (s *Stage) Threshold() float64 { return s.thr }
func (s *Stage) RefMic() int        { return s.ref }
func (s *Stage) WakeScore() float64 { return s.wakeScore }

// Push feeds one batch of cancelled 16kHz samples per microphone (equal
// lengths, any size). Covariances are always updated; once locked, the gated
// beam for every completed hop is returned (it lags the input by one hop plus
// GateDelay).
func (s *Stage) Push(mics [][]int16) []int16 {
	if len(mics) != s.m {
		return nil
	}
	for c := 0; c < s.m; c++ {
		for _, v := range mics[c] {
			s.pend[c] = append(s.pend[c], float64(v))
		}
	}
	var out []int16
	for len(s.pend[0]) >= hopH {
		out = append(out, s.hop()...)
		for c := 0; c < s.m; c++ {
			s.pend[c] = s.pend[c][hopH:]
		}
	}
	for c := 0; c < s.m; c++ { // keep the backing arrays from growing without bound
		if cap(s.pend[c]) > 8*hopH && len(s.pend[c]) < hopH {
			s.pend[c] = append([]float64(nil), s.pend[c]...)
		}
	}
	return out
}

func (s *Stage) hop() []int16 {
	m := s.m
	for c := 0; c < m; c++ {
		re, im := s.re[c], s.im[c]
		copy(re, s.tail[c])
		copy(re[hopH:], s.pend[c][:hopH])
		copy(s.tail[c], s.pend[c][:hopH])
		for i := range re {
			re[i] *= s.win[i]
			im[i] = 0
		}
		s.fft.forward(re, im)
		for k := 0; k < bins; k++ {
			s.xb[k][c] = complex(re[k], im[k])
		}
	}
	// Covariance accumulation.
	for k := 0; k < bins; k++ {
		x := s.xb[k]
		blk := s.cur[k*m*m : (k+1)*m*m]
		for i := 0; i < m; i++ {
			for j := 0; j < m; j++ {
				blk[i*m+j] += complex64(x[i] * cmplx.Conj(x[j]))
			}
		}
	}
	s.curN++
	if s.curN == blockFrames {
		copy(s.ring[s.head], s.cur)
		for i := range s.cur {
			s.cur[i] = 0
		}
		s.curN = 0
		s.head = (s.head + 1) % len(s.ring)
		if s.filled < len(s.ring) {
			s.filled++
		}
		if s.locked && !s.ended {
			s.detect()
		}
	}
	if !s.locked {
		return nil
	}
	// Beam, inverse FFT, synthesis window, overlap-add.
	re, im := s.re[0], s.im[0]
	for k := 0; k < bins; k++ {
		var y complex128
		for c := 0; c < m; c++ {
			y += cmplx.Conj(s.h[k][c]) * s.xb[k][c]
		}
		re[k], im[k] = real(y), imag(y)
		if k > 0 && k < bins-1 {
			re[frameN-k], im[frameN-k] = real(y), -imag(y)
		}
	}
	s.fft.inverse(re, im)
	for i := 0; i < frameN; i++ {
		s.ola[i] += re[i] * s.win[i]
	}
	// Gate: push this hop into the delay line, take out the oldest hop.
	target := gateFloor
	if s.hold > 0 {
		target = 1
	}
	out := make([]int16, hopH)
	for i := 0; i < hopH; i++ {
		s.gain += (target - s.gain) * 0.01 // ~6ms at 16kHz
		out[i] = clip16(s.dly[i] * s.gain)
	}
	copy(s.dly, s.dly[hopH:])
	copy(s.dly[GateDelay-hopH:], s.ola[:hopH])
	copy(s.ola, s.ola[hopH:])
	for i := hopH; i < frameN; i++ {
		s.ola[i] = 0
	}
	return out
}

// blockAgo returns the block sum n blocks back (0 = the newest complete one).
func (s *Stage) blockAgo(n int) []complex64 {
	return s.ring[(s.head-1-n+2*len(s.ring))%len(s.ring)]
}

// sumBlocks averages blocks [from, from+count) back into a bins×m×m matrix.
func (s *Stage) sumBlocks(from, count int) [][]complex128 {
	m := s.m
	out := make([][]complex128, bins)
	for k := range out {
		out[k] = make([]complex128, m*m)
	}
	for b := from; b < from+count; b++ {
		blk := s.blockAgo(b)
		for k := 0; k < bins; k++ {
			o := out[k]
			src := blk[k*m*m : (k+1)*m*m]
			for i := range o {
				o[i] += complex128(src[i])
			}
		}
	}
	n := complex(float64(count*blockFrames), 0)
	for k := range out {
		for i := range out[k] {
			out[k][i] /= n
		}
	}
	return out
}

var errTooEarly = errors.New("turnbeam: not enough audio before the wake word")

// Lock is called at the wake crossing: the last WakeS is the talker, the
// NoiseS before it is the room. It computes the beam and the detector's
// threshold; Push then returns the beam's output.
func (s *Stage) Lock() error {
	m := s.m
	wakeB := int(math.Round(s.cfg.WakeS / blockS))
	noiseB := int(math.Round(s.cfg.NoiseS / blockS))
	if avail := s.filled - wakeB; avail < noiseB {
		noiseB = avail
	}
	if float64(noiseB)*blockS < 1 {
		return errTooEarly
	}
	rx := s.sumBlocks(0, wakeB)
	rn := s.sumBlocks(wakeB, noiseB)
	lo, hi := bandLoHz*frameN/sampleRate, bandHiHz*frameN/sampleRate

	// Reference mic: most talker over noise in the speech band.
	best := math.Inf(-1)
	for c := 0; c < m; c++ {
		var a, b float64
		for k := lo; k <= hi; k++ {
			a += real(rx[k][c*m+c])
			b += real(rn[k][c*m+c])
		}
		if r := a / (b + 1e-12); r > best {
			best, s.ref = r, c
		}
	}

	s.h = make([][]complex128, bins)
	s.whL = make([][]complex128, bins)
	s.whU = make([][]complex128, bins)
	for k := 0; k < bins; k++ {
		n := loaded(rn[k], m, s.cfg.Load)
		l, err := cholesky(n, m)
		if err != nil { // a dead bin (DC, silence): pass the reference mic through
			s.h[k] = make([]complex128, m)
			s.h[k][s.ref] = 1
			continue
		}
		u, _ := principal(whiten(l, m, rx[k]), m)
		d := make([]complex128, m)
		matvec(l, m, u, d) // d = L·u
		if d[s.ref] == 0 {
			s.h[k] = make([]complex128, m)
			s.h[k][s.ref] = 1
			continue
		}
		r := d[s.ref]
		for i := range d {
			d[i] /= r
		}
		x := solveUpperH(l, m, solveLower(l, m, d)) // n⁻¹·d
		den := dot(d, x)
		h := make([]complex128, m)
		for i := range h {
			h[i] = x[i] / cmplx.Conj(den)
		}
		s.h[k] = h
		s.whL[k], s.whU[k] = l, u
	}

	// Threshold from the pre-wake windows (two blocks, hop one), as the
	// offline prototype set it: 1.15 × the 99th percentile, at least 1.5.
	var base []float64
	for b := wakeB; b+2 <= wakeB+noiseB; b++ {
		base = append(base, s.windowScore(b))
	}
	sort.Float64s(base)
	p99 := base[int(math.Min(float64(len(base)-1), math.Ceil(0.99*float64(len(base)))-1))]
	s.preP99 = p99
	s.thr = 0
	// Seed the floor with the windows just before the wake word, oldest
	// first: a talker who runs the command on from the wake word never lets
	// the turn's own floor see the TV alone. They age out after floorS.
	nf := int(math.Round(floorS / blockS))
	s.floorHist = s.floorHist[:0]
	for b := wakeB + nf - 1; b >= wakeB; b-- {
		if b+2 <= wakeB+noiseB {
			s.floorHist = append(s.floorHist, s.windowScore(b))
		}
	}
	s.wakeScore = s.windowScore(0)
	s.Trace = s.Trace[:0]

	s.dly = make([]float64, GateDelay)
	s.hold, s.gain = gateRelease, 1 // the wake word's tail passes; the command may run on from it
	s.locked, s.ended = true, false
	s.sinceS, s.lastHit, s.score = 0, -1, 0
	for i := range s.ola {
		s.ola[i] = 0
	}
	return nil
}

// windowScore is the detector over blocks [from, from+2): the median, over
// the speech band, of the share of whitened power along the talker's
// whitened steering vector, relative to the mean per channel.
func (s *Stage) windowScore(from int) float64 {
	m := s.m
	rc := s.sumBlocks(from, 2)
	lo, hi := bandLoHz*frameN/sampleRate, bandHiHz*frameN/sampleRate
	vals := make([]float64, 0, hi-lo+1)
	w := make([]complex128, m)
	for k := lo; k <= hi; k++ {
		if s.whL[k] == nil {
			continue
		}
		wm := whiten(s.whL[k], m, rc[k])
		tr := traceRe(wm, m) / float64(m)
		if tr <= 0 {
			continue
		}
		matvec(wm, m, s.whU[k], w)
		vals = append(vals, real(dot(s.whU[k], w))/tr)
	}
	if len(vals) == 0 {
		return 0
	}
	sort.Float64s(vals)
	return vals[len(vals)/2]
}

func (s *Stage) detect() {
	s.sinceS += blockS
	s.score = s.windowScore(0)
	s.Trace = append(s.Trace, s.score)
	// Minimum statistics (Martin 2001): the TV's own score drifts once the
	// turn starts (0.8 in a take whose pre-wake windows set 1.65, 41C8
	// 2026-11-21), so presence is judged against the lowest score of the
	// last floorS, not against the pre-wake level.
	s.floorHist = append(s.floorHist, s.score)
	n := int(math.Round(floorS / blockS))
	lo := len(s.floorHist) - n
	if lo < 0 {
		lo = 0
	}
	floor := s.floorHist[lo]
	for _, v := range s.floorHist[lo:] {
		floor = math.Min(floor, v)
	}
	s.thr = presenceRatio * floor
	if s.score > s.thr {
		s.hold = gateRelease
	} else {
		s.hold -= blockS
	}
	// A window that still holds the wake word's tail is the wake word, not
	// the command: counting it would end a turn whose talker pauses for the
	// cue before speaking.
	if s.sinceS < 3*blockS-1e-9 { // two-block window + the partial block at Lock
		return
	}
	if s.score > s.thr {
		s.lastHit = s.sinceS
		return
	}
	if s.lastHit < 0 {
		if s.sinceS >= s.cfg.StartS {
			s.ended = true
		}
		return
	}
	if s.sinceS-s.lastHit >= s.cfg.HangS {
		s.ended = true
	}
}

// Unlock returns to listening: the covariances keep accumulating.
func (s *Stage) Unlock() {
	s.locked, s.ended = false, false
	s.h, s.whL, s.whU = nil, nil, nil
}

func loaded(a []complex128, m int, share float64) []complex128 {
	out := append([]complex128(nil), a...)
	add := complex(share*traceRe(a, m)/float64(m), 0)
	for i := 0; i < m; i++ {
		out[i*m+i] += add
	}
	return out
}

func clip16(v float64) int16 {
	if v > 32767 {
		return 32767
	}
	if v < -32768 {
		return -32768
	}
	return int16(math.Round(v))
}
