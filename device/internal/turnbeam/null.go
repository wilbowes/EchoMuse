package turnbeam

import "math/cmplx"

// The idle TV null: a second output of the stage, for wake scoring before any
// wake. Per bin it tracks the dominant spatial direction of the room over the
// last ~nullTauS — with the TV on, the TV — and removes it, keeping the
// first microphone's view of everything else:
//
//	y = x₀ − v₀·(vᴴx),  v = principal eigenvector of the running covariance
//
// One power iteration per hop, warm-started from the last hop's v, tracks a
// direction that moves at room speed. The talker's own 0.7s wake word barely
// moves an 8s average, so it survives the null unless they stand in line
// with the TV, which is why it is scored beside the raw mics, never instead.
//
// Offline on 41C8, lounge, TV on, 3 m (2026-11-21): best of four raw mics
// woke 4/16 and 8/10 on two takes; the null alone 9/16 and 6/10; the two
// together 10/16 and 9/10; no TV peak above 0.3 on either. Removing the top
// two directions removed the talker too (2/16).
const (
	nullTauS = 8.0
	nullGain = 2.0 // the projection takes the TV's share of the level with it
)

type nullState struct {
	r      [][]complex128 // [bin] m×m running covariance
	v      [][]complex128 // [bin] dominant direction, unit
	w      []complex128
	re, im []float64
	ola    []float64
	out    []int16
}

// EnableNull starts the idle TV null; its output accumulates for TakeNull.
func (s *Stage) EnableNull() {
	if s.nl != nil {
		return
	}
	m := s.m
	n := &nullState{r: make([][]complex128, bins), v: make([][]complex128, bins), w: make([]complex128, m),
		re: make([]float64, frameN), im: make([]float64, frameN), ola: make([]float64, frameN)}
	for k := range n.r {
		n.r[k] = make([]complex128, m*m)
		n.v[k] = make([]complex128, m)
		n.v[k][0] = 1
	}
	s.nl = n
}

// TakeNull returns the null's output since the last call (it lags the input
// by one hop), or nil when the null is off.
func (s *Stage) TakeNull() []int16 {
	if s.nl == nil {
		return nil
	}
	out := s.nl.out
	s.nl.out = nil
	return out
}

// nullHop runs on the current hop's spectra (s.xb).
func (s *Stage) nullHop() {
	n, m := s.nl, s.m
	a := 1 - float64(hopH)/sampleRate/nullTauS
	for k := 0; k < bins; k++ {
		x, r, v := s.xb[k], n.r[k], n.v[k]
		// Project with the direction learnt before this hop.
		var p complex128
		for c := 0; c < m; c++ {
			p += cmplx.Conj(v[c]) * x[c]
		}
		y := x[0] - v[0]*p
		if k == 0 || k == bins-1 {
			y = complex(real(y), 0)
		}
		n.re[k], n.im[k] = real(y), imag(y)
		if k > 0 && k < bins-1 {
			n.re[frameN-k], n.im[frameN-k] = real(y), -imag(y)
		}
		for i := 0; i < m; i++ {
			for j := 0; j < m; j++ {
				r[i*m+j] = complex(a, 0)*r[i*m+j] + complex(1-a, 0)*x[i]*cmplx.Conj(x[j])
			}
		}
		matvec(r, m, v, n.w)
		if normalise(n.w) > 0 {
			copy(v, n.w)
		}
	}
	s.fft.inverse(n.re, n.im)
	for i := 0; i < frameN; i++ {
		n.ola[i] += n.re[i] * s.win[i]
	}
	for i := 0; i < hopH; i++ {
		n.out = append(n.out, clip16(n.ola[i]*nullGain))
	}
	copy(n.ola, n.ola[hopH:])
	for i := hopH; i < frameN; i++ {
		n.ola[i] = 0
	}
}
