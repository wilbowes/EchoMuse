package outchain

import "math"

// fft is an in-place iterative radix-2 Cooley-Tukey FFT (decimation in
// time). len(a) must be a power of 2 — the one caller (eqFIR) only ever
// calls it at the fixed size it precomputed, so that is asserted once
// there rather than checked on every call.
//
// There is no FFT in the Go standard library and this project deliberately
// carries no DSP dependency (device/CLAUDE.md: new deps must cross-compile
// cleanly against the FireOS 5 sysroot, and a pure-Go implementation this
// size is lower risk than vetting a third party's). Correctness here rests
// entirely on fft_test.go, not on this comment — in particular on
// comparing convolution results against an INDEPENDENT naive convolution,
// not merely a round-trip of this same function.
func fft(a []complex128, inverse bool) {
	n := len(a)
	if n <= 1 {
		return
	}

	// Bit-reversal permutation.
	for i, j := 1, 0; i < n; i++ {
		bit := n >> 1
		for ; j&bit != 0; bit >>= 1 {
			j ^= bit
		}
		j ^= bit
		if i < j {
			a[i], a[j] = a[j], a[i]
		}
	}

	for length := 2; length <= n; length <<= 1 {
		angle := -2 * math.Pi / float64(length)
		if inverse {
			angle = -angle
		}
		wlen := complex(math.Cos(angle), math.Sin(angle))
		half := length / 2
		for i := 0; i < n; i += length {
			w := complex(1.0, 0.0)
			for j := 0; j < half; j++ {
				u := a[i+j]
				v := a[i+j+half] * w
				a[i+j] = u + v
				a[i+j+half] = u - v
				w *= wlen
			}
		}
	}

	if inverse {
		scale := complex(float64(n), 0)
		for i := range a {
			a[i] /= scale
		}
	}
}

// isPow2 reports whether n is a positive power of 2.
func isPow2(n int) bool {
	return n > 0 && n&(n-1) == 0
}

// nextPow2 is the smallest power of 2 >= n.
func nextPow2(n int) int {
	p := 1
	for p < n {
		p <<= 1
	}
	return p
}

// realFFT transforms a real block of n samples (n a power of 2, at least 4)
// through a complex FFT of n/2: the even samples as the real part, the odd
// as the imaginary, separated afterwards with one twiddle per bin. Only the
// half spectrum (bins 0..n/2) is produced and consumed — the rest is its
// mirror for a real signal. Every twiddle is computed once, here.
type realFFT struct {
	n, half int
	tw      []complex128 // exp(-2πik/half), k < half/2: the half-size FFT's
	post    []complex128 // exp(-2πik/n), k <= half: the even/odd split's
	rev     []int        // bit reversal for the half-size FFT
	z       []complex128 // scratch, half long
}

func newRealFFT(n int) *realFFT {
	half := n / 2
	r := &realFFT{
		n: n, half: half,
		tw:   make([]complex128, half/2),
		post: make([]complex128, half+1),
		rev:  make([]int, half),
		z:    make([]complex128, half),
	}
	for k := range r.tw {
		a := -2 * math.Pi * float64(k) / float64(half)
		r.tw[k] = complex(math.Cos(a), math.Sin(a))
	}
	for k := range r.post {
		a := -2 * math.Pi * float64(k) / float64(n)
		r.post[k] = complex(math.Cos(a), math.Sin(a))
	}
	bits := 0
	for 1<<bits < half {
		bits++
	}
	for i := range r.rev {
		v := 0
		for b := 0; b < bits; b++ {
			if i&(1<<b) != 0 {
				v |= 1 << (bits - 1 - b)
			}
		}
		r.rev[i] = v
	}
	return r
}

// cfft is the half-size complex FFT in place; inverse uses conjugate
// twiddles and leaves the result unscaled.
func (r *realFFT) cfft(a []complex128, inverse bool) {
	for i, j := range r.rev {
		if i < j {
			a[i], a[j] = a[j], a[i]
		}
	}
	n := r.half
	for length := 2; length <= n; length <<= 1 {
		h := length / 2
		step := n / length
		for i := 0; i < n; i += length {
			for j := 0; j < h; j++ {
				w := r.tw[j*step]
				if inverse {
					w = complex(real(w), -imag(w))
				}
				u := a[i+j]
				v := a[i+j+h] * w
				a[i+j] = u + v
				a[i+j+h] = u - v
			}
		}
	}
}

// forward writes the half spectrum of x (len n) to out (len n/2+1).
func (r *realFFT) forward(x []float64, out []complex128) {
	z := r.z
	for i := range z {
		z[i] = complex(x[2*i], x[2*i+1])
	}
	r.cfft(z, false)
	h := r.half
	for k := 0; k <= h; k++ {
		zk := z[k%h]
		zm := z[(h-k)%h]
		zmc := complex(real(zm), -imag(zm))
		even := (zk + zmc) * 0.5
		odd := (zk - zmc) * complex(0, -0.5)
		out[k] = even + r.post[k]*odd
	}
}

// inverse writes the real block (len n) whose half spectrum is spec, scaled
// so that inverse(forward(x)) == x.
func (r *realFFT) inverse(spec []complex128, out []float64) {
	z := r.z
	h := r.half
	for k := 0; k < h; k++ {
		yk := spec[k]
		ym := spec[h-k]
		ymc := complex(real(ym), -imag(ym))
		even := (yk + ymc) * 0.5
		odd := (yk - ymc) * 0.5
		p := r.post[k]
		z[k] = even + complex(0, 1)*complex(real(p), -imag(p))*odd
	}
	r.cfft(z, true)
	scale := 1 / float64(h)
	for i, v := range z {
		out[2*i] = real(v) * scale
		out[2*i+1] = imag(v) * scale
	}
}
