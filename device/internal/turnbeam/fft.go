package turnbeam

import "math"

// fft is an in-place radix-2 complex FFT of a fixed power-of-two size, with
// its bit-reversal and twiddle tables built once. Pure Go so the stage runs
// and tests on the host; at 512 points and four to seven channels every
// 16ms it is a small fraction of a core.
type fft struct {
	n   int
	rev []int
	cos []float64
	sin []float64
}

func newFFT(n int) *fft {
	f := &fft{n: n, rev: make([]int, n), cos: make([]float64, n/2), sin: make([]float64, n/2)}
	bits := 0
	for 1<<bits < n {
		bits++
	}
	for i := range f.rev {
		r := 0
		for b := 0; b < bits; b++ {
			if i&(1<<b) != 0 {
				r |= 1 << (bits - 1 - b)
			}
		}
		f.rev[i] = r
	}
	for i := range f.cos {
		a := -2 * math.Pi * float64(i) / float64(n)
		f.cos[i], f.sin[i] = math.Cos(a), math.Sin(a)
	}
	return f
}

// forward transforms re/im in place (no scaling). inverse does the same with
// conjugate twiddles and scales by 1/n.
func (f *fft) forward(re, im []float64) { f.run(re, im, 1) }

func (f *fft) inverse(re, im []float64) {
	f.run(re, im, -1)
	s := 1 / float64(f.n)
	for i := range re {
		re[i] *= s
		im[i] *= s
	}
}

func (f *fft) run(re, im []float64, sign float64) {
	n := f.n
	for i, r := range f.rev {
		if i < r {
			re[i], re[r] = re[r], re[i]
			im[i], im[r] = im[r], im[i]
		}
	}
	for size := 2; size <= n; size <<= 1 {
		half, step := size/2, n/size
		for start := 0; start < n; start += size {
			for k := 0; k < half; k++ {
				wr, wi := f.cos[k*step], sign*f.sin[k*step]
				a, b := start+k, start+k+half
				tr := wr*re[b] - wi*im[b]
				ti := wr*im[b] + wi*re[b]
				re[b], im[b] = re[a]-tr, im[a]-ti
				re[a], im[a] = re[a]+tr, im[a]+ti
			}
		}
	}
}
