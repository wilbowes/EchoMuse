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
// over a TV that plays throughout. The beam must take the TV well down
// against the talker, and the endpoint must fire after the talker stops and
// not before.
func TestTheBeamFavoursTheTalkerAndTheTurnEndsWithThem(t *testing.T) {
	r := rand.New(rand.NewSource(3))
	const pre, wake, cmd, after = 7.0, 1.6, 1.5, 4.0
	n := int((pre + wake + cmd + after) * sampleRate)
	talkFrom, talkTo := int(pre*sampleRate), int((pre+wake+cmd)*sampleRate)
	sc := makeScene(r, n, func(i int) bool { return i >= talkFrom && i < talkTo })
	all := sc.mics(r, 0, n, true)

	s := New(Config{Mics: 4})
	batch := 2560
	lockAt := int((pre + wake) * sampleRate)
	var out []int16
	var refIn []int16
	endedAt, early := -1.0, false
	for i := 0; i+batch <= n; i += batch {
		chunk := make([][]int16, 4)
		for c := range chunk {
			chunk[c] = all[c][i : i+batch]
		}
		if i == lockAt/batch*batch && !s.Locked() {
			if err := s.Lock(); err != nil {
				t.Fatal(err)
			}
		}
		y := s.Push(chunk)
		if s.Locked() {
			out = append(out, y...)
			refIn = append(refIn, chunk[s.RefMic()][:len(y)]...)
			if s.Ended() && endedAt < 0 {
				endedAt = float64(i+batch)/sampleRate - (pre + wake + cmd)
				early = i+batch <= talkTo
			}
		}
	}
	// Output and input from the lock: command = first cmd seconds, TV tail after +1s.
	c0, c1 := 0, int(cmd*sampleRate)
	t0 := int((cmd + 1) * sampleRate)
	inSIR := 10 * math.Log10(power(refIn[c0:c1])/power(refIn[t0:]))
	outSIR := 10 * math.Log10(power(out[c0:c1])/power(out[t0:]))
	t.Logf("talker over TV: ref mic %.1f dB, beam %.1f dB; threshold %.2f; ended %.2fs after the talker stopped",
		inSIR, outSIR, s.Threshold(), endedAt)
	if outSIR-inSIR < 10 {
		t.Errorf("the beam gained %.1f dB of talker over TV, want 10", outSIR-inSIR)
	}
	if endedAt < 0 || endedAt > s.cfg.HangS+0.5 {
		t.Errorf("turn ended %.2fs after the talker stopped, want by %.2fs", endedAt, s.cfg.HangS+0.5)
	}
	if early {
		t.Errorf("turn ended before the talker stopped")
	}
}

// Nobody speaks after the wake word: the turn ends at StartS, not at the cap.
func TestATurnWithNothingAfterTheWakeEndsAtStartS(t *testing.T) {
	r := rand.New(rand.NewSource(4))
	const pre, wake = 7.0, 1.6
	n := int((pre + wake + 5) * sampleRate)
	talkFrom, talkTo := int(pre*sampleRate), int((pre+wake)*sampleRate)
	sc := makeScene(r, n, func(i int) bool { return i >= talkFrom && i < talkTo })
	all := sc.mics(r, 0, n, true)
	s := New(Config{Mics: 4})
	batch := 2560
	lockAt := (talkTo + batch - 1) / batch * batch // the crossing comes after the wake word
	endedAt := -1.0
	for i := 0; i+batch <= n; i += batch {
		chunk := make([][]int16, 4)
		for c := range chunk {
			chunk[c] = all[c][i : i+batch]
		}
		if i == lockAt && !s.Locked() {
			if err := s.Lock(); err != nil {
				t.Fatal(err)
			}
		}
		s.Push(chunk)
		if s.Locked() && s.Ended() && endedAt < 0 {
			endedAt = float64(i+batch-lockAt) / sampleRate
		}
	}
	if endedAt < 0 || math.Abs(endedAt-s.cfg.StartS) > 0.4 {
		t.Fatalf("a turn with nothing after the wake ended at %.2fs, want ~%.1fs", endedAt, s.cfg.StartS)
	}
}

func TestLockRefusesWithoutEnoughRoom(t *testing.T) {
	s := New(Config{Mics: 4})
	s.Push([][]int16{make([]int16, 8000), make([]int16, 8000), make([]int16, 8000), make([]int16, 8000)})
	if err := s.Lock(); err == nil {
		t.Fatal("locked with half a second of audio")
	}
}
