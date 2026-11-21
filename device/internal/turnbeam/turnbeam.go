// Package turnbeam is the wake-anchored turn stage: once the wake word has
// fired, it beams the turn toward the talker and cleans the beam's output,
// using only the room's own spatial statistics. Where the command starts and
// stops is decided outside, by a speech detector on this output (Segmenter).
//
// Everything is from published methods old enough to be settled practice:
//
//   - Covariances: per-bin spatial covariances of the cancelled mics, kept as
//     128ms block sums. Noise = the NoiseS before the wake word (with the TV
//     on, the TV as the array hears it); talker = the WakeS holding the wake
//     word, ending at the crossing.
//   - Steering by covariance subtraction: the principal eigenvector of
//     (talker − noise) per bin, made distortionless on the talker's best mic.
//   - MVDR (Capon 1969) with diagonal loading, fixed for the turn.
//   - Post-filter: a Wiener gain on a decision-directed a priori SNR (Ephraim
//     & Malah 1984) with a -15 dB floor (Berouti 1979). Its noise PSD follows
//     the TV moment to moment from the blocking reference (Griffiths & Jim
//     1982): the mics with the talker's direction projected out, scaled by
//     the beam's static residual noise.
//
// Offline on 41C8 in the lounge with the TV on (12 commands, 2026-11-21,
// docs/history/device.md), with Silero segmenting this output: 10/12 exact,
// 15% WER, and no TV words in any of 6 wakes with no command after them; the
// in-turn presence gate this replaces got 6/12 and leaked in 3 of 6.
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

	// Post-filter.
	pfFloor   = 0.1778 // -15 dB
	pfAlpha   = 0.98   // decision-directed smoothing
	pfXiMin   = 0.0031623
	phiSmooth = 0.6  // one-pole on the blocking power per hop (~40ms)
	phiMin    = 0.25 // the moving noise PSD never drops under 1/4 of the static one

	// At Lock the beam is replayed over the last ReplayS before the crossing,
	// so the post-filter starts warm, the floor is measured on the beam's own
	// pre-wake output, and the output can start before the lock (OutLeadS
	// before the crossing).
	ReplayS  = 3.0
	OutLeadS = 0.3
	floorTo  = -1.6 // pre-wake floor over [-ReplayS, floorTo] around the crossing
	maxLeadS = 0.6  // crossing to Lock, at most
	// FloorFrame is the frame the floor is measured over: the device's Silero
	// step, so the Segmenter compares like with like.
	FloorFrame = 640
)

// Config sets the stage's windows. Zero fields take the defaults.
type Config struct {
	Mics   int     // cancelled microphone channels, in the board's order
	NoiseS float64 // pre-wake noise window (default 6.5s)
	WakeS  float64 // window before the crossing that holds the wake word (default 1.6s)
	Load   float64 // diagonal loading, share of the mean noise power (default 1e-3)
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
}

// Stage holds the STFT state, the covariance blocks, the recent input and,
// once locked, the beam and its post-filter. Not safe for concurrent use: one
// capture goroutine owns it.
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

	// The consumed input of the last ReplayS+maxLeadS (+ one hop), per mic,
	// as a circular buffer of whole hops.
	in      [][]int16
	inHead  int // next write position (samples)
	inCount int

	// Scratch per frame.
	re, im [][]float64
	xb     [][]complex128 // [bin][mic]
	xw     []complex128   // whitened input, one bin

	locked    bool
	replaying bool // Lock's replay: no covariance accumulation, no input ring
	h         [][]complex128 // [bin][mic] beam weights
	ref       int
	ola       []float64
	whL       [][]complex128 // [bin] Cholesky of the loaded noise covariance
	whU       [][]complex128 // [bin] whitened unit steering
	phiS      []float64      // [bin] the beam's static residual noise PSD, 1/(dᴴRn⁻¹d)
	pz        []float64      // [bin] smoothed blocking power
	pzWarm    bool
	prev      []float64 // [bin] last |Ŝ|², for the decision-directed SNR
	phi       []float64 // scratch
	floorDB   float64
}

// New returns an unlocked stage.
func New(cfg Config) *Stage {
	cfg.defaults()
	m := cfg.Mics
	s := &Stage{cfg: cfg, m: m, fft: newFFT(frameN), win: make([]float64, frameN)}
	for i := range s.win {
		s.win[i] = math.Sqrt(0.5 - 0.5*math.Cos(2*math.Pi*float64(i)/frameN))
	}
	nBlocks := int(math.Ceil((cfg.NoiseS+cfg.WakeS+maxLeadS)/blockS)) + 2
	s.ring = make([][]complex64, nBlocks)
	for i := range s.ring {
		s.ring[i] = make([]complex64, bins*m*m)
	}
	s.cur = make([]complex64, bins*m*m)
	s.tail = make([][]float64, m)
	s.pend = make([][]float64, m)
	s.re = make([][]float64, m)
	s.im = make([][]float64, m)
	inN := (int((ReplayS+maxLeadS)*sampleRate)/hopH + 2) * hopH
	s.in = make([][]int16, m)
	for i := 0; i < m; i++ {
		s.tail[i] = make([]float64, hopH)
		s.re[i] = make([]float64, frameN)
		s.im[i] = make([]float64, frameN)
		s.in[i] = make([]int16, inN)
	}
	s.xb = make([][]complex128, bins)
	for k := range s.xb {
		s.xb[k] = make([]complex128, m)
	}
	s.xw = make([]complex128, m)
	s.ola = make([]float64, frameN)
	s.phi = make([]float64, bins)
	return s
}

// Locked, RefMic and FloorDB are the stage's observables. FloorDB is the
// median level (dB, S16 scale) of the cleaned beam over the pre-wake TV, in
// FloorFrame frames: what "nothing but the room" sounds like at the output.
func (s *Stage) Locked() bool      { return s.locked }
func (s *Stage) RefMic() int       { return s.ref }
func (s *Stage) FloorDB() float64  { return s.floorDB }

// Push feeds one batch of cancelled 16kHz samples per microphone (equal
// lengths, any size). Covariances are always updated; once locked, the
// cleaned beam for every completed hop is returned (it lags the input by one
// hop).
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
	in := make([][]float64, s.m)
	for len(s.pend[0]) >= hopH {
		for c := range in {
			in[c] = s.pend[c][:hopH]
		}
		out = append(out, s.hop(in)...)
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

// hop runs one STFT hop on hopH new samples per mic.
func (s *Stage) hop(in [][]float64) []int16 {
	m := s.m
	if !s.replaying {
		n := len(s.in[0])
		for c := 0; c < m; c++ {
			for i, v := range in[c] {
				s.in[c][(s.inHead+i)%n] = int16(v)
			}
		}
		s.inHead = (s.inHead + hopH) % n
		if s.inCount < n {
			s.inCount += hopH
		}
	}
	for c := 0; c < m; c++ {
		re, im := s.re[c], s.im[c]
		copy(re, s.tail[c])
		copy(re[hopH:], in[c])
		copy(s.tail[c], in[c])
		for i := range re {
			re[i] *= s.win[i]
			im[i] = 0
		}
		s.fft.forward(re, im)
		for k := 0; k < bins; k++ {
			s.xb[k][c] = complex(re[k], im[k])
		}
	}
	if !s.replaying {
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
		}
	}
	if !s.locked {
		return nil
	}
	return s.beamHop()
}

// beamHop applies the beam and the post-filter to the current hop (s.xb) and
// returns the next hopH output samples.
func (s *Stage) beamHop() []int16 {
	m := s.m
	// Blocking power per bin: whiten x, remove its component along the
	// whitened steering; what is left is noise only.
	for k := 0; k < bins; k++ {
		if s.whL[k] == nil {
			continue
		}
		l, u, x := s.whL[k], s.whU[k], s.xb[k]
		for i := 0; i < m; i++ { // xw = L⁻¹x
			v := x[i]
			for j := 0; j < i; j++ {
				v -= l[i*m+j] * s.xw[j]
			}
			s.xw[i] = v / l[i*m+i]
		}
		var p complex128
		for i := 0; i < m; i++ {
			p += cmplx.Conj(u[i]) * s.xw[i]
		}
		pz := 0.0
		for i := 0; i < m; i++ {
			pz += sq(s.xw[i] - u[i]*p)
		}
		pz /= float64(m - 1)
		if s.pzWarm {
			s.pz[k] = phiSmooth*s.pz[k] + (1-phiSmooth)*pz
		} else {
			s.pz[k] = pz
		}
	}
	s.pzWarm = true
	for k := 0; k < bins; k++ { // ±1 bin smoothing
		a, b := k-1, k+1
		if a < 0 {
			a = 0
		}
		if b > bins-1 {
			b = bins - 1
		}
		s.phi[k] = (s.pz[a] + s.pz[k] + s.pz[b]) / 3
	}
	re, im := s.re[0], s.im[0]
	for k := 0; k < bins; k++ {
		var y complex128
		for c := 0; c < m; c++ {
			y += cmplx.Conj(s.h[k][c]) * s.xb[k][c]
		}
		if s.whL[k] != nil {
			pv := math.Max(s.phi[k]*s.phiS[k], phiMin*s.phiS[k])
			gam := sq(y) / math.Max(pv, 1e-12)
			xi := math.Max(pfAlpha*s.prev[k]+(1-pfAlpha)*math.Max(gam-1, 0), pfXiMin)
			g := math.Max(xi/(1+xi), pfFloor)
			s.prev[k] = g * g * gam
			y *= complex(g, 0)
		}
		if k == 0 || k == bins-1 {
			y = complex(real(y), 0)
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
	out := make([]int16, hopH)
	for i := 0; i < hopH; i++ {
		out[i] = clip16(s.ola[i])
	}
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

// Lock is called once a wake opens a session, leadS after the crossing (the
// scorer's and the session's latency; clamped to [0, 0.6]s). The WakeS
// before the crossing is the talker, the NoiseS before that the room. It
// computes the beam, replays it over the last ReplayS before the crossing,
// and returns the cleaned beam from LockStart() up to now; Push then
// continues it sample for sample.
func (s *Stage) Lock(leadS float64) ([]int16, error) {
	m := s.m
	leadS = math.Max(0, math.Min(maxLeadS, leadS))
	skipB := int(math.Round(leadS / blockS))
	wakeB := int(math.Round(s.cfg.WakeS / blockS))
	noiseB := int(math.Round(s.cfg.NoiseS / blockS))
	if avail := s.filled - skipB - wakeB; avail < noiseB {
		noiseB = avail
	}
	replayN := (int((leadS+ReplayS)*sampleRate) + hopH - 1) / hopH * hopH
	if float64(noiseB)*blockS < 1 || s.inCount < replayN+hopH {
		return nil, errTooEarly
	}
	rx := s.sumBlocks(skipB, wakeB)
	rn := s.sumBlocks(skipB+wakeB, noiseB)
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
	s.phiS = make([]float64, bins)
	for k := 0; k < bins; k++ {
		s.h[k] = make([]complex128, m)
		s.h[k][s.ref] = 1 // a dead bin (DC, silence) passes the reference mic through
		n := loaded(rn[k], m, s.cfg.Load)
		l, err := cholesky(n, m)
		if err != nil {
			continue
		}
		d := subtractSteer(rx[k], rn[k], m)
		if d == nil || cmplx.Abs(d[s.ref]) < 1e-12 {
			continue
		}
		r := d[s.ref]
		for i := range d {
			d[i] /= r
		}
		x := solveUpperH(l, m, solveLower(l, m, d)) // n⁻¹·d
		den := real(dot(d, x))
		if den <= 0 {
			continue
		}
		for i := range x {
			s.h[k][i] = x[i] / complex(den, 0)
		}
		u := solveLower(l, m, d) // whitened steering, unit
		if normalise(u) == 0 {
			continue
		}
		s.whL[k], s.whU[k], s.phiS[k] = l, u, 1/den
	}

	// Replay the recent input through the locked beam.
	s.pz, s.prev, s.pzWarm = make([]float64, bins), make([]float64, bins), false
	for i := range s.ola {
		s.ola[i] = 0
	}
	n := len(s.in[0])
	start := (s.inHead - replayN + 2*n) % n
	saved := make([][]float64, m)
	for c := 0; c < m; c++ {
		saved[c] = append([]float64(nil), s.tail[c]...)
		for i := 0; i < hopH; i++ {
			s.tail[c][i] = float64(s.in[c][(start-hopH+i+n)%n])
		}
	}
	s.locked, s.replaying = true, true
	var y []int16
	hopIn := make([][]float64, m)
	for c := range hopIn {
		hopIn[c] = make([]float64, hopH)
	}
	for off := 0; off < replayN; off += hopH {
		for c := 0; c < m; c++ {
			for i := 0; i < hopH; i++ {
				hopIn[c][i] = float64(s.in[c][(start+off+i)%n])
			}
		}
		y = append(y, s.hop(hopIn)...)
	}
	s.replaying = false
	for c := 0; c < m; c++ { // the replay ends on the same hop the live input did
		copy(s.tail[c], saved[c])
	}
	// y[i] is replay-input sample i - hopH (the beam lags one hop: the first
	// hop emits the hop before the replay's start); the crossing is leadS
	// before the replay's end.
	cross := replayN - int(math.Round(leadS*sampleRate)) // in replay-input samples
	at := func(t float64) int { // output index of time t around the crossing
		return cross + int(math.Round(t*sampleRate)) + hopH
	}
	var lv []float64
	for i := at(-ReplayS) + hopH; i+FloorFrame <= at(floorTo); i += FloorFrame { // skip the first, half-windowed hop
		if i < 0 {
			continue
		}
		lv = append(lv, LevelDB(y[i:i+FloorFrame]))
	}
	s.floorDB = -90
	if len(lv) > 0 {
		sort.Float64s(lv)
		s.floorDB = lv[len(lv)/2]
	}
	// Everything from the replay's second hop (the first is half-windowed):
	// the caller warms its speech detector on it up to -OutLeadS.
	return y[hopH:], nil
}

// LockStart is the time, around the crossing, of the first sample Lock
// returns.
func LockStart() float64 { return -ReplayS + float64(hopH)/sampleRate }

// LevelDB is the mean-square level of S16 samples in dB.
func LevelDB(x []int16) float64 {
	e := 0.0
	for _, v := range x {
		e += float64(v) * float64(v)
	}
	return 10 * math.Log10(e/float64(len(x))+1e-9)
}

// subtractSteer is the principal eigenvector of rx - rn: the talker's own
// covariance, with the room's taken out (covariance subtraction).
func subtractSteer(rx, rn []complex128, m int) []complex128 {
	a := make([]complex128, m*m)
	for i := range a {
		a[i] = rx[i] - rn[i]
	}
	return hermEigMax(a, m)
}

// Unlock returns to listening: the covariances keep accumulating.
func (s *Stage) Unlock() {
	s.locked = false
	s.h, s.whL, s.whU, s.phiS = nil, nil, nil, nil
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
