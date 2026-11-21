package turnbeam

import (
	"errors"
	"math"
	"math/cmplx"
)

// Small dense Hermitian algebra for one frequency bin: m×m matrices stored
// row-major in []complex128, m = the board's microphone count (4 or 7).

var errNotPD = errors.New("turnbeam: covariance not positive definite")

// cholesky returns lower-triangular L with a = L·Lᴴ.
func cholesky(a []complex128, m int) ([]complex128, error) {
	l := make([]complex128, m*m)
	for j := 0; j < m; j++ {
		s := real(a[j*m+j])
		for k := 0; k < j; k++ {
			v := l[j*m+k]
			s -= real(v)*real(v) + imag(v)*imag(v)
		}
		if s <= 0 || math.IsNaN(s) {
			return nil, errNotPD
		}
		d := math.Sqrt(s)
		l[j*m+j] = complex(d, 0)
		for i := j + 1; i < m; i++ {
			v := a[i*m+j]
			for k := 0; k < j; k++ {
				v -= l[i*m+k] * cmplx.Conj(l[j*m+k])
			}
			l[i*m+j] = v / complex(d, 0)
		}
	}
	return l, nil
}

// solveLower solves L·x = b; solveUpperH solves Lᴴ·x = b.
func solveLower(l []complex128, m int, b []complex128) []complex128 {
	x := make([]complex128, m)
	for i := 0; i < m; i++ {
		v := b[i]
		for k := 0; k < i; k++ {
			v -= l[i*m+k] * x[k]
		}
		x[i] = v / l[i*m+i]
	}
	return x
}

func solveUpperH(l []complex128, m int, b []complex128) []complex128 {
	x := make([]complex128, m)
	for i := m - 1; i >= 0; i-- {
		v := b[i]
		for k := i + 1; k < m; k++ {
			v -= cmplx.Conj(l[k*m+i]) * x[k]
		}
		x[i] = v / cmplx.Conj(l[i*m+i])
	}
	return x
}

// whiten returns W = L⁻¹·a·L⁻ᴴ.
func whiten(l []complex128, m int, a []complex128) []complex128 {
	// Y = L⁻¹·a, column by column; then W = L⁻¹·Yᴴ (a and W are Hermitian).
	y := make([]complex128, m*m)
	col := make([]complex128, m)
	for j := 0; j < m; j++ {
		for i := 0; i < m; i++ {
			col[i] = a[i*m+j]
		}
		x := solveLower(l, m, col)
		for i := 0; i < m; i++ {
			y[i*m+j] = x[i]
		}
	}
	w := make([]complex128, m*m)
	for j := 0; j < m; j++ {
		for i := 0; i < m; i++ {
			col[i] = cmplx.Conj(y[j*m+i])
		}
		x := solveLower(l, m, col)
		for i := 0; i < m; i++ {
			w[i*m+j] = x[i]
		}
	}
	return w
}

// principal returns the unit eigenvector of Hermitian a with the largest
// eigenvalue, by power iteration from the column of largest norm, and its
// Rayleigh quotient.
func principal(a []complex128, m int) ([]complex128, float64) {
	v := make([]complex128, m)
	best, bj := -1.0, 0
	for j := 0; j < m; j++ {
		s := 0.0
		for i := 0; i < m; i++ {
			s += sq(a[i*m+j])
		}
		if s > best {
			best, bj = s, j
		}
	}
	for i := 0; i < m; i++ {
		v[i] = a[i*m+bj]
	}
	normalise(v)
	w := make([]complex128, m)
	for it := 0; it < 60; it++ {
		matvec(a, m, v, w)
		if normalise(w) == 0 {
			break
		}
		v, w = w, v
	}
	matvec(a, m, v, w)
	lam := real(dot(v, w))
	return v, lam
}

func matvec(a []complex128, m int, v, out []complex128) {
	for i := 0; i < m; i++ {
		var s complex128
		for k := 0; k < m; k++ {
			s += a[i*m+k] * v[k]
		}
		out[i] = s
	}
}

// dot is aᴴ·b.
func dot(a, b []complex128) complex128 {
	var s complex128
	for i := range a {
		s += cmplx.Conj(a[i]) * b[i]
	}
	return s
}

func normalise(v []complex128) float64 {
	s := 0.0
	for _, x := range v {
		s += sq(x)
	}
	n := math.Sqrt(s)
	if n == 0 {
		return 0
	}
	for i := range v {
		v[i] /= complex(n, 0)
	}
	return n
}

func sq(x complex128) float64 { return real(x)*real(x) + imag(x)*imag(x) }

func traceRe(a []complex128, m int) float64 {
	s := 0.0
	for i := 0; i < m; i++ {
		s += real(a[i*m+i])
	}
	return s
}
