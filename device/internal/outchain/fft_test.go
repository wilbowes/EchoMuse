package outchain

import (
	"math"
	"math/cmplx"
	"math/rand"
	"testing"
)

const fftTol = 1e-9

func approxEqualC(a, b complex128, tol float64) bool {
	return cmplx.Abs(a-b) <= tol
}

// naiveDFT is the textbook O(N^2) definition, independent of fft() — the
// ground truth the fast version is checked against, not just a second
// call to the same code.
func naiveDFT(a []complex128, inverse bool) []complex128 {
	n := len(a)
	out := make([]complex128, n)
	sign := -1.0
	if inverse {
		sign = 1.0
	}
	for k := 0; k < n; k++ {
		var sum complex128
		for t := 0; t < n; t++ {
			angle := sign * 2 * math.Pi * float64(k) * float64(t) / float64(n)
			sum += a[t] * complex(math.Cos(angle), math.Sin(angle))
		}
		if inverse {
			sum /= complex(float64(n), 0)
		}
		out[k] = sum
	}
	return out
}

func TestFFTMatchesNaiveDFT(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	for _, n := range []int{1, 2, 4, 8, 16, 64, 256} {
		a := make([]complex128, n)
		for i := range a {
			a[i] = complex(rng.NormFloat64(), rng.NormFloat64())
		}
		want := naiveDFT(a, false)
		got := append([]complex128(nil), a...)
		fft(got, false)
		for i := range got {
			if !approxEqualC(got[i], want[i], fftTol) {
				t.Fatalf("n=%d bin %d: fft=%v naiveDFT=%v", n, i, got[i], want[i])
			}
		}
	}
}

func TestFFTInverseMatchesNaiveInverseDFT(t *testing.T) {
	rng := rand.New(rand.NewSource(2))
	n := 128
	a := make([]complex128, n)
	for i := range a {
		a[i] = complex(rng.NormFloat64(), rng.NormFloat64())
	}
	want := naiveDFT(a, true)
	got := append([]complex128(nil), a...)
	fft(got, true)
	for i := range got {
		if !approxEqualC(got[i], want[i], fftTol) {
			t.Fatalf("bin %d: fft inverse=%v naive inverse=%v", i, got[i], want[i])
		}
	}
}

func TestFFTRoundTrip(t *testing.T) {
	rng := rand.New(rand.NewSource(3))
	for _, n := range []int{1, 2, 8, 64, 4096} {
		a := make([]complex128, n)
		for i := range a {
			a[i] = complex(rng.NormFloat64(), 0)
		}
		orig := append([]complex128(nil), a...)
		fft(a, false)
		fft(a, true)
		for i := range a {
			if !approxEqualC(a[i], orig[i], fftTol) {
				t.Fatalf("n=%d index %d: round trip got %v want %v", n, i, a[i], orig[i])
			}
		}
	}
}

func TestFFTImpulseIsFlatSpectrum(t *testing.T) {
	n := 32
	a := make([]complex128, n)
	a[0] = 1
	fft(a, false)
	for i, v := range a {
		if !approxEqualC(v, 1, fftTol) {
			t.Fatalf("bin %d: got %v, want 1 (impulse must have a flat spectrum)", i, v)
		}
	}
}

func TestFFTConstantIsImpulseSpectrum(t *testing.T) {
	n := 32
	a := make([]complex128, n)
	for i := range a {
		a[i] = 1
	}
	fft(a, false)
	if !approxEqualC(a[0], complex(float64(n), 0), fftTol) {
		t.Fatalf("bin 0: got %v, want %v", a[0], n)
	}
	for i := 1; i < n; i++ {
		if !approxEqualC(a[i], 0, fftTol) {
			t.Fatalf("bin %d: got %v, want 0", i, a[i])
		}
	}
}

// The actual use case: convolution via the FFT's multiply-spectra
// property, checked against an INDEPENDENT naive O(N*M) convolution —
// this is the test that matters most, since it is exactly what eqFIR
// relies on.
func TestFFTConvolutionMatchesNaiveConvolution(t *testing.T) {
	rng := rand.New(rand.NewSource(4))
	x := make([]float64, 100)
	h := make([]float64, 30)
	for i := range x {
		x[i] = rng.NormFloat64()
	}
	for i := range h {
		h[i] = rng.NormFloat64() * 0.1
	}

	// Naive linear convolution, full length len(x)+len(h)-1.
	wantLen := len(x) + len(h) - 1
	want := make([]float64, wantLen)
	for i := range x {
		for j := range h {
			want[i+j] += x[i] * h[j]
		}
	}

	n := nextPow2(wantLen)
	xa := make([]complex128, n)
	ha := make([]complex128, n)
	for i, v := range x {
		xa[i] = complex(v, 0)
	}
	for i, v := range h {
		ha[i] = complex(v, 0)
	}
	fft(xa, false)
	fft(ha, false)
	for i := range xa {
		xa[i] *= ha[i]
	}
	fft(xa, true)

	for i := 0; i < wantLen; i++ {
		got := real(xa[i])
		if math.Abs(got-want[i]) > 1e-9 {
			t.Fatalf("index %d: fft-conv=%v naive-conv=%v", i, got, want[i])
		}
	}
}

func TestIsPow2(t *testing.T) {
	cases := map[int]bool{0: false, 1: true, 2: true, 3: false, 4: true, 4095: false, 4096: true}
	for n, want := range cases {
		if got := isPow2(n); got != want {
			t.Errorf("isPow2(%d) = %v, want %v", n, got, want)
		}
	}
}

func TestNextPow2(t *testing.T) {
	cases := map[int]int{1: 1, 2: 2, 3: 4, 4: 4, 5: 8, 4095: 4096, 4096: 4096, 4097: 8192}
	for n, want := range cases {
		if got := nextPow2(n); got != want {
			t.Errorf("nextPow2(%d) = %d, want %d", n, got, want)
		}
	}
}
