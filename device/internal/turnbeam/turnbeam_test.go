package turnbeam

import (
	"math"
	"math/cmplx"
	"math/rand"
	"testing"
)

func TestFFTMatchesTheDFT(t *testing.T) {
	r := rand.New(rand.NewSource(1))
	f := newFFT(frameN)
	re, im := make([]float64, frameN), make([]float64, frameN)
	x := make([]complex128, frameN)
	for i := range re {
		re[i], im[i] = r.NormFloat64(), r.NormFloat64()
		x[i] = complex(re[i], im[i])
	}
	f.forward(re, im)
	worst := 0.0
	for k := 0; k < frameN; k += 37 {
		var s complex128
		for n := 0; n < frameN; n++ {
			s += x[n] * cmplx.Exp(complex(0, -2*math.Pi*float64(k*n)/frameN))
		}
		worst = math.Max(worst, cmplx.Abs(s-complex(re[k], im[k])))
	}
	if worst > 1e-9 {
		t.Fatalf("FFT differs from the DFT by %g", worst)
	}
	f.inverse(re, im)
	for i := range re {
		if math.Abs(re[i]-real(x[i])) > 1e-9 || math.Abs(im[i]-imag(x[i])) > 1e-9 {
			t.Fatalf("inverse(forward(x)) != x at %d", i)
		}
	}
}

func TestCholeskyWhitensToTheIdentity(t *testing.T) {
	const m = 4
	r := rand.New(rand.NewSource(2))
	a := make([]complex128, m*m)
	for s := 0; s < 20; s++ { // a = Σ v·vᴴ, positive definite
		v := make([]complex128, m)
		for i := range v {
			v[i] = complex(r.NormFloat64(), r.NormFloat64())
		}
		for i := 0; i < m; i++ {
			for j := 0; j < m; j++ {
				a[i*m+j] += v[i] * cmplx.Conj(v[j])
			}
		}
	}
	l, err := cholesky(a, m)
	if err != nil {
		t.Fatal(err)
	}
	w := whiten(l, m, a)
	for i := 0; i < m; i++ {
		for j := 0; j < m; j++ {
			want := complex(0, 0)
			if i == j {
				want = 1
			}
			if cmplx.Abs(w[i*m+j]-want) > 1e-9 {
				t.Fatalf("L⁻¹·a·L⁻ᴴ[%d][%d] = %v", i, j, w[i*m+j])
			}
		}
	}
}

// scene is a four-mic far-field toy: a talker and a TV, each a point source
// with integer per-mic delays, plus a little independent noise per mic.
type scene struct {
	talker, tv [][]float64
}

func delayed(src []float64, d int) []float64 {
	out := make([]float64, len(src))
	copy(out[d:], src[:len(src)-d])
	return out
}

func makeScene(r *rand.Rand, n int, talkOn func(i int) bool) scene {
	talk, tv := make([]float64, n), make([]float64, n)
	for i := range talk {
		tv[i] = 900 * r.NormFloat64()
		if talkOn(i) {
			talk[i] = 2500 * r.NormFloat64()
		}
	}
	sc := scene{}
	for _, dt := range []int{0, 1, 2, 1} { // talker from one side
		sc.talker = append(sc.talker, delayed(talk, dt))
	}
	for _, dv := range []int{3, 1, 0, 2} { // TV from another
		sc.tv = append(sc.tv, delayed(tv, dv))
	}
	return sc
}

func (sc scene) mics(r *rand.Rand, from, to int, withTalker bool) [][]int16 {
	out := make([][]int16, 4)
	for c := range out {
		out[c] = make([]int16, to-from)
		for i := from; i < to; i++ {
			v := sc.tv[c][i] + 30*r.NormFloat64()
			if withTalker {
				v += sc.talker[c][i]
			}
			out[c][i-from] = clip16(v)
		}
	}
	return out
}

func power(x []int16) float64 {
	s := 0.0
	for _, v := range x {
		s += float64(v) * float64(v)
	}
	return s / float64(len(x))
}

// The talker speaks for 1.6s (the wake word) and 1.5s more (the command)
// over a TV that plays throughout. The cleaned beam must take the TV well
// down against the talker; Lock's replay must start the output OutLeadS
// before the crossing and hand over to Push without a gap; and the pre-wake
// floor must describe the TV-only output.
func TestTheBeamFavoursTheTalker(t *testing.T) {
	r := rand.New(rand.NewSource(3))
	const pre, wake, cmd, after = 7.0, 1.6, 1.5, 4.0
	n := int((pre + wake + cmd + after) * sampleRate)
	talkFrom, talkTo := int(pre*sampleRate), int((pre+wake+cmd)*sampleRate)
	sc := makeScene(r, n, func(i int) bool { return i >= talkFrom && i < talkTo })
	all := sc.mics(r, 0, n, true)

	s := New(Config{Mics: 4})
	batch := 2560
	cross := int((pre + wake) * sampleRate) // the crossing: the wake word ends
	lockAt := (cross/batch + 1) * batch     // a batch later
	lead := float64(lockAt-cross) / sampleRate
	var out []int16
	for i := 0; i+batch <= n; i += batch {
		if i == lockAt {
			y, t0, err := s.Lock(lead)
			if err != nil {
				t.Fatal(err)
			}
			if t0 > -ReplayS+1e-9 || t0 < -ReplayS-float64(hopH)/sampleRate {
				t.Fatalf("Lock's output starts at %.4fs, want within a hop before -%.1fs", t0, ReplayS)
			}
			want := int((lead-t0)*sampleRate) - hopH
			if d := len(y) - want; d < -hopH || d > hopH {
				t.Fatalf("Lock returned %d samples, want ~%d (from %.2fs around the crossing)", len(y), want, t0)
			}
			out = append(out, y[int(math.Round((-OutLeadS-t0)*sampleRate)):]...)
		}
		chunk := make([][]int16, 4)
		for c := range chunk {
			chunk[c] = all[c][i : i+batch]
		}
		out = append(out, s.Push(chunk)...)
	}
	// out[0] is OutLeadS before the crossing; the input sample at the same
	// time is cross - OutLeadS·sr.
	at := func(sec float64) int { return int((OutLeadS + sec) * sampleRate) }
	in0 := cross - int(OutLeadS*sampleRate)
	ref := all[s.RefMic()]
	cmdIn, tvIn := ref[in0+at(0):in0+at(cmd)], ref[in0+at(cmd+1):in0+at(cmd+3.5)]
	cmdOut, tvOut := out[at(0):at(cmd)], out[at(cmd+1):at(cmd+3.5)]
	inSIR := 10 * math.Log10(power(cmdIn)/power(tvIn))
	outSIR := 10 * math.Log10(power(cmdOut)/power(tvOut))
	t.Logf("talker over TV: ref mic %.1f dB, cleaned beam %.1f dB; floor %.1f dB, TV-only out %.1f dB",
		inSIR, outSIR, s.FloorDB(), LevelDB(tvOut))
	if outSIR-inSIR < 10 {
		t.Errorf("the cleaned beam gained %.1f dB of talker over TV, want 10", outSIR-inSIR)
	}
	// The talker must come through at their own level: the post-filter's
	// floor applies to the TV, not to them.
	if g := 10 * math.Log10(power(cmdOut)/power(cmdIn)); g < -3 {
		t.Errorf("the talker came out %.1f dB down", g)
	}
	if d := s.FloorDB() - LevelDB(tvOut); math.Abs(d) > 3 {
		t.Errorf("pre-wake floor %.1f dB against %.1f dB of TV-only output", s.FloorDB(), LevelDB(tvOut))
	}
	// No gap at the hand-over from the replay to Push: the output over the
	// lock is as loud as the command around it.
	for _, w := range [][2]float64{{lead - 0.05, lead + 0.05}} {
		if g := LevelDB(out[at(w[0]):at(w[1])]) - LevelDB(cmdOut); g < -6 {
			t.Errorf("the output drops %.1f dB where the replay hands over to Push", g)
		}
	}
}

func TestLockRefusesWithoutEnoughRoom(t *testing.T) {
	s := New(Config{Mics: 4})
	s.Push([][]int16{make([]int16, 8000), make([]int16, 8000), make([]int16, 8000), make([]int16, 8000)})
	if _, _, err := s.Lock(0); err == nil {
		t.Fatal("locked with half a second of audio")
	}
}

// The steering by covariance subtraction finds the talker's direction even
// when the noise is far louder than the talker.
func TestSubtractSteerFindsAWeakTalker(t *testing.T) {
	m := 4
	tv := []complex128{1, 1i, -1, -1i}
	tk := []complex128{1, -1, 1, -1}
	rn, rx := make([]complex128, m*m), make([]complex128, m*m)
	for i := 0; i < m; i++ {
		for j := 0; j < m; j++ {
			n := 100*tv[i]*cmplx.Conj(tv[j])
			if i == j {
				n += 1
			}
			rn[i*m+j] = n
			rx[i*m+j] = n + 0.5*tk[i]*cmplx.Conj(tk[j])
		}
	}
	d := subtractSteer(rx, rn, m)
	var c complex128
	for i := range d {
		c += cmplx.Conj(tk[i]) * d[i]
	}
	if a := cmplx.Abs(c) / 2; a < 0.99 { // |tk| = 2, |d| = 1
		t.Fatalf("steering against the talker's direction: |cos| = %.3f", a)
	}
}

// hermEigMax: a·v = λmax·v on random Hermitian matrices, including ones with
// a negative eigenvalue larger in magnitude than the top one.
func TestHermEigMaxFindsTheLargestEigenvalue(t *testing.T) {
	r := rand.New(rand.NewSource(9))
	for trial := 0; trial < 50; trial++ {
		m := 4 + trial%4
		b := make([]complex128, m*m)
		for i := range b {
			b[i] = complex(r.NormFloat64(), r.NormFloat64())
		}
		a := make([]complex128, m*m) // a = b + bᴴ - 3·tr shift makes a big negative eigenvalue likely
		for i := 0; i < m; i++ {
			for j := 0; j < m; j++ {
				a[i*m+j] = b[i*m+j] + cmplx.Conj(b[j*m+i])
			}
		}
		v := hermEigMax(a, m)
		w := make([]complex128, m)
		matvec(a, m, v, w)
		lam := real(dot(v, w))
		res := 0.0
		for i := range w {
			res += sq(w[i] - complex(lam, 0)*v[i])
		}
		if math.Sqrt(res) > 1e-9*math.Max(1, math.Abs(lam)) {
			t.Fatalf("m=%d: ‖a·v - λv‖ = %.3g", m, math.Sqrt(res))
		}
		// λ must be the largest: the Rayleigh quotient of any vector is below it.
		for k := 0; k < 20; k++ {
			x := make([]complex128, m)
			for i := range x {
				x[i] = complex(r.NormFloat64(), r.NormFloat64())
			}
			normalise(x)
			matvec(a, m, x, w)
			if q := real(dot(x, w)); q > lam+1e-9 {
				t.Fatalf("m=%d: a vector has Rayleigh quotient %.4f over λ=%.4f", m, q, lam)
			}
		}
	}
}
