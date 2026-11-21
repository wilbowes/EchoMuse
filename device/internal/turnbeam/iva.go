package turnbeam

import (
	"math"
	"math/cmplx"
)

// Online blind source separation for wake scoring before any wake: AuxIVA
// (Ono 2011) with a spherical Laplace source model and iterative-projection
// updates, run causally one hop at a time (Taniguchi et al. 2014). It splits
// the microphones into as many streams as there are microphones without
// knowing where anything is — the TV, the talker, a radio — and each stream,
// projected back to the first microphone's level, can be scored for the wake
// word. Nothing is learnt across turns and nothing about any voice is kept.
//
// Per bin f and source k, with W the demixing matrix (rows wₖᴴ):
//
//	yₖ = wₖᴴx,  rₖ = ‖yₖ‖ over all bins (this hop)
//	Vₖ ← a·Vₖ + (1−a)·xxᴴ/rₖ
//	wₖ ← (W·Vₖ)⁻¹eₖ,  wₖ ← wₖ / √(wₖᴴVₖwₖ)
//	output yₖ·(W⁻¹)₀ₖ  (projection back to mic 0)
//
// Offline on 41C8, lounge, TV on, 3 m (four captures, 37 attempts,
// 2026-11-21): best of four raw mics woke 18, the separated streams 31 (the
// batch algorithm 30). Both TV false wakes in ~8 min of TV were on separated
// streams, at 0.6-0.7: a clean TV-dialogue stream is what separation also
// produces, so the bar for these streams is a soak's to set.
const (
	ivaTauS = 2.0
	ivaEps  = 1e-6
	// The covariances take every ivaCovEvery-th hop and the demixing is
	// re-solved every ivaSolveEvery-th; outputs go out every hop. Every hop
	// for both cost 127ms per 160ms batch on 41C8 (one A53 core at 80%).
	// Offline, covariance every 2nd and solve every 4th caught 27 of 37
	// against 31 (with the raw mics, 29).
	ivaCovEvery   = 1
	ivaSolveEvery = 2
	// loudTauS smooths each stream's level for picking the steady loudest
	// one (with the TV on, the TV), which the caller may leave unscored.
	loudTauS = 10.0
)

type ivaState struct {
	w      [][]complex128   // [bin] m×m demixing, row k = wₖᴴ
	v      [][][]complex128 // [source][bin] m×m weighted covariance
	y      [][]complex128   // [bin][source] this hop's outputs
	r      []float64
	xx     []complex128   // m×m scratch (xxᴴ, shared by the sources)
	a0     [][]complex128 // [bin] row 0 of W⁻¹ (projection back), refreshed on each solve
	hops   int
	scr    []complex128 // m×m scratch (W·V)
	inv    []complex128 // m×m scratch (W⁻¹)
	rhs    []complex128
	re, im [][]float64 // [source] synthesis scratch
	ola    [][]float64
	out    [][]int16
	loud   []float64 // [source] smoothed level (power)
}

// EnableIVA starts the online separation; its streams accumulate for
// TakeIVA.
func (s *Stage) EnableIVA() {
	if s.iv != nil {
		return
	}
	m := s.m
	iv := &ivaState{
		w: make([][]complex128, bins), v: make([][][]complex128, m), y: make([][]complex128, bins),
		r: make([]float64, m), xx: make([]complex128, m*m), a0: make([][]complex128, bins), scr: make([]complex128, m*m), inv: make([]complex128, m*m), rhs: make([]complex128, m),
		re: make([][]float64, m), im: make([][]float64, m), ola: make([][]float64, m), out: make([][]int16, m),
		loud: make([]float64, m),
	}
	for k := 0; k < bins; k++ {
		iv.w[k] = make([]complex128, m*m)
		for i := 0; i < m; i++ {
			iv.w[k][i*m+i] = 1
		}
		iv.y[k] = make([]complex128, m)
		iv.a0[k] = make([]complex128, m)
		iv.a0[k][0] = 1 // W = I
	}
	for src := 0; src < m; src++ {
		iv.v[src] = make([][]complex128, bins)
		for k := 0; k < bins; k++ {
			iv.v[src][k] = make([]complex128, m*m)
			for i := 0; i < m; i++ {
				iv.v[src][k][i*m+i] = 1e-2
			}
		}
		iv.re[src] = make([]float64, frameN)
		iv.im[src] = make([]float64, frameN)
		iv.ola[src] = make([]float64, frameN)
	}
	s.iv = iv
}

// TakeIVA returns each separated stream's output since the last call (one
// hop behind the input), or nil when the separation is off.
func (s *Stage) TakeIVA() [][]int16 {
	if s.iv == nil {
		return nil
	}
	out := s.iv.out
	s.iv.out = make([][]int16, s.m)
	return out
}

// IVALoudest is the stream with the highest smoothed level, -1 when off.
func (s *Stage) IVALoudest() int {
	if s.iv == nil {
		return -1
	}
	best, bi := -1.0, -1
	for k, l := range s.iv.loud {
		if l > best {
			best, bi = l, k
		}
	}
	return bi
}

func (s *Stage) ivaHop() {
	iv, m := s.iv, s.m
	cov := iv.hops%ivaCovEvery == 0
	solve := iv.hops%ivaSolveEvery == 0
	iv.hops++
	if cov {
		// Each source's norm over bins with the current demixing.
		for src := range iv.r {
			iv.r[src] = 0
		}
		for k := 0; k < bins; k++ {
			x, w := s.xb[k], iv.w[k]
			for src := 0; src < m; src++ {
				var acc complex128
				for j := 0; j < m; j++ {
					acc += w[src*m+j] * x[j]
				}
				iv.r[src] += sq(acc)
			}
		}
		for src := range iv.r {
			iv.r[src] = math.Sqrt(iv.r[src]) + ivaEps
		}
	}
	a := 1 - float64(hopH*ivaCovEvery)/sampleRate/ivaTauS
	for k := 0; k < bins; k++ {
		x, w := s.xb[k], iv.w[k]
		if cov {
			for i := 0; i < m; i++ {
				for j := 0; j < m; j++ {
					iv.xx[i*m+j] = x[i] * cmplx.Conj(x[j])
				}
			}
			for src := 0; src < m; src++ {
				v, g := iv.v[src][k], complex((1-a)/iv.r[src], 0)
				for i := range v {
					v[i] = complex(a, 0)*v[i] + g*iv.xx[i]
				}
			}
		}
		if solve {
			for src := 0; src < m; src++ {
				s.ivaSolve(k, src)
			}
			copy(iv.inv, w)
			if invertInPlace(iv.inv, m) {
				copy(iv.a0[k], iv.inv[:m])
			}
		}
		// Outputs with the current demixing, projected back to mic 0.
		for src := 0; src < m; src++ {
			var acc complex128
			for j := 0; j < m; j++ {
				acc += w[src*m+j] * x[j]
			}
			iv.y[k][src] = acc * iv.a0[k][src]
		}
	}
	// Synthesis per stream, and its smoothed level.
	la := 1 - float64(hopH)/sampleRate/loudTauS
	for src := 0; src < m; src++ {
		re, im := iv.re[src], iv.im[src]
		p := 0.0
		for k := 0; k < bins; k++ {
			y := iv.y[k][src]
			if k == 0 || k == bins-1 {
				y = complex(real(y), 0)
			}
			p += sq(y)
			re[k], im[k] = real(y), imag(y)
			if k > 0 && k < bins-1 {
				re[frameN-k], im[frameN-k] = real(y), -imag(y)
			}
		}
		iv.loud[src] = la*iv.loud[src] + (1-la)*p
		s.fft.inverse(re, im)
		ola := iv.ola[src]
		for i := 0; i < frameN; i++ {
			ola[i] += re[i] * s.win[i]
		}
		for i := 0; i < hopH; i++ {
			iv.out[src] = append(iv.out[src], clip16(ola[i]))
		}
		copy(ola, ola[hopH:])
		for i := hopH; i < frameN; i++ {
			ola[i] = 0
		}
	}
}

// ivaSolve is the iterative-projection update of source src's row in bin k.
func (s *Stage) ivaSolve(k, src int) {
	iv, m := s.iv, s.m
	w, v := iv.w[k], iv.v[src][k]
	for i := 0; i < m; i++ { // scr = W·V (rows of w are already wᴴ)
		for j := 0; j < m; j++ {
			var acc complex128
			for l := 0; l < m; l++ {
				acc += w[i*m+l] * v[l*m+j]
			}
			iv.scr[i*m+j] = acc
		}
	}
	for i := range iv.rhs {
		iv.rhs[i] = 0
	}
	iv.rhs[src] = 1
	if !solveInPlace(iv.scr, m, iv.rhs) {
		return
	}
	var q complex128 // uᴴVu
	for i := 0; i < m; i++ {
		var vu complex128
		for j := 0; j < m; j++ {
			vu += v[i*m+j] * iv.rhs[j]
		}
		q += cmplx.Conj(iv.rhs[i]) * vu
	}
	n := math.Sqrt(math.Max(real(q), 0))
	if n == 0 {
		return
	}
	in := complex(1/n, 0)
	for j := 0; j < m; j++ {
		w[src*m+j] = cmplx.Conj(iv.rhs[j]) * in
	}
}

// solveInPlace solves a·u = b for u (into b) by Gaussian elimination with
// partial pivoting; a (m×m, row-major) is destroyed. False when singular.
func solveInPlace(a []complex128, m int, b []complex128) bool {
	for c := 0; c < m; c++ {
		p, best := c, sq(a[c*m+c])
		for r := c + 1; r < m; r++ {
			if v := sq(a[r*m+c]); v > best {
				p, best = r, v
			}
		}
		if best < 1e-300 {
			return false
		}
		if p != c {
			for j := 0; j < m; j++ {
				a[c*m+j], a[p*m+j] = a[p*m+j], a[c*m+j]
			}
			b[c], b[p] = b[p], b[c]
		}
		rp := 1 / a[c*m+c]
		for r := c + 1; r < m; r++ {
			f := a[r*m+c] * rp
			if f == 0 {
				continue
			}
			for j := c; j < m; j++ {
				a[r*m+j] -= f * a[c*m+j]
			}
			b[r] -= f * b[c]
		}
	}
	for r := m - 1; r >= 0; r-- {
		acc := b[r]
		for j := r + 1; j < m; j++ {
			acc -= a[r*m+j] * b[j]
		}
		b[r] = acc * (1 / a[r*m+r])
	}
	return true
}

// invertInPlace replaces a (m×m) with its inverse (Gauss-Jordan, partial
// pivoting). False when singular (a is then undefined).
func invertInPlace(a []complex128, m int) bool {
	var inv [64]complex128 // m ≤ 8
	if m > 8 {
		return false
	}
	for i := 0; i < m; i++ {
		for j := 0; j < m; j++ {
			inv[i*m+j] = 0
		}
		inv[i*m+i] = 1
	}
	for c := 0; c < m; c++ {
		p, best := c, sq(a[c*m+c])
		for r := c + 1; r < m; r++ {
			if v := sq(a[r*m+c]); v > best {
				p, best = r, v
			}
		}
		if best < 1e-300 {
			return false
		}
		if p != c {
			for j := 0; j < m; j++ {
				a[c*m+j], a[p*m+j] = a[p*m+j], a[c*m+j]
				inv[c*m+j], inv[p*m+j] = inv[p*m+j], inv[c*m+j]
			}
		}
		d := 1 / a[c*m+c]
		for j := 0; j < m; j++ {
			a[c*m+j] *= d
			inv[c*m+j] *= d
		}
		for r := 0; r < m; r++ {
			if r == c {
				continue
			}
			f := a[r*m+c]
			if f == 0 {
				continue
			}
			for j := 0; j < m; j++ {
				a[r*m+j] -= f * a[c*m+j]
				inv[r*m+j] -= f * inv[c*m+j]
			}
		}
	}
	copy(a, inv[:m*m])
	return true
}
