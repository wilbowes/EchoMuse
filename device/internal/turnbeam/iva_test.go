package turnbeam

import (
	"math"
	"math/cmplx"
	"math/rand"
	"testing"
)

// A steady source (the TV) and a bursty one (the talker: 1s on, 1s off, with
// a syllable-rate envelope) from different directions, as integer-sample
// delays across four mics. After the separation settles, one stream must hold
// the talker far further above the TV than any microphone does.
func TestIVASeparatesATalkerFromASteadySource(t *testing.T) {
	const n = 2560 * 125 // 20 s in whole 160ms batches
	rng := rand.New(rand.NewSource(2))
	tv := make([]float64, n+8)
	talk := make([]float64, n+8)
	on := make([]bool, n+8)
	for i := range tv {
		tv[i] = rng.NormFloat64() * 2000
	}
	for i := 16000 * 4; i < n; i++ {
		if (i/16000)%2 == 0 {
			env := 0.5 + 0.5*math.Sin(2*math.Pi*4*float64(i)/16000)
			talk[i] = rng.NormFloat64() * 2000 * env
			on[i] = true
		}
	}
	tvDelay := []int{0, 1, 2, 3}
	talkDelay := []int{3, 1, 0, 2}
	mics := make([][]int16, 4)
	for c := range mics {
		mics[c] = make([]int16, n)
		for i := 0; i < n; i++ {
			v := tv[i+8-tvDelay[c]] + talk[i+8-talkDelay[c]]
			mics[c][i] = int16(math.Max(-32768, math.Min(32767, v)))
		}
	}
	s := New(Config{Mics: 4})
	s.EnableIVA()
	streams := make([][]int16, 4)
	for off := 0; off < n; off += 2560 {
		s.Push([][]int16{mics[0][off : off+2560], mics[1][off : off+2560], mics[2][off : off+2560], mics[3][off : off+2560]})
		for k, y := range s.TakeIVA() {
			streams[k] = append(streams[k], y...)
		}
	}
	// Talker-over-TV from the energy with the talker on against off, over the
	// last 8 s (settled), whole 1s halves trimmed by 0.2s at each edge.
	sir := func(x []int16, lag int) float64 {
		var eOn, eOff float64
		var nOn, nOff int
		for i := 16000 * 12; i < n-lag; i++ {
			in := i % 16000
			if in < 3200 || in > 12800 {
				continue
			}
			v := float64(x[i+lag])
			if on[i] {
				eOn += v * v
				nOn++
			} else {
				eOff += v * v
				nOff++
			}
		}
		pOn, pOff := eOn/float64(nOn), eOff/float64(nOff)
		if pOn < 1e4 { // a near-silent stream says nothing about separation
			return math.Inf(-1)
		}
		return 10 * math.Log10(math.Max(pOn-pOff, 1e-9)/math.Max(pOff, 1))
	}
	bestMic := math.Inf(-1)
	for c := range mics {
		bestMic = math.Max(bestMic, sir(mics[c], 0))
	}
	bestStream := math.Inf(-1)
	for k := range streams {
		if len(streams[k]) < n-hopH {
			t.Fatalf("stream %d gave %d samples for %d in", k, len(streams[k]), n)
		}
		bestStream = math.Max(bestStream, sir(streams[k], hopH))
	}
	t.Logf("talker over TV: best mic %.1f dB, best stream %.1f dB", bestMic, bestStream)
	if gain := bestStream - bestMic; gain < 10 {
		t.Errorf("talker over TV: best stream %.1f dB, best mic %.1f dB (gain %.1f, want at least 10)", bestStream, bestMic, gain)
	}
	if l := s.IVALoudest(); l < 0 || l > 3 {
		t.Errorf("loudest stream %d", l)
	}
}

func TestIVAOffGivesNothing(t *testing.T) {
	s := New(Config{Mics: 2})
	s.Push([][]int16{make([]int16, 2560), make([]int16, 2560)})
	if s.TakeIVA() != nil || s.IVALoudest() != -1 {
		t.Fatal("separation output with it off")
	}
}

func TestSolveAndInvert(t *testing.T) {
	a := []complex128{2, 1i, 0, 1, 3, -1, 0.5i, 0, 1 + 1i}
	x := []complex128{1, -2i, 0.5}
	b := make([]complex128, 3)
	for i := 0; i < 3; i++ {
		for j := 0; j < 3; j++ {
			b[i] += a[i*3+j] * x[j]
		}
	}
	ac := append([]complex128(nil), a...)
	if !solveInPlace(ac, 3, b) {
		t.Fatal("solve: singular")
	}
	for i := range x {
		if cmplx.Abs(b[i]-x[i]) > 1e-12 {
			t.Fatalf("solve: got %v want %v", b, x)
		}
	}
	inv := append([]complex128(nil), a...)
	if !invertInPlace(inv, 3) {
		t.Fatal("invert: singular")
	}
	for i := 0; i < 3; i++ {
		for j := 0; j < 3; j++ {
			var acc complex128
			for l := 0; l < 3; l++ {
				acc += a[i*3+l] * inv[l*3+j]
			}
			want := complex128(0)
			if i == j {
				want = 1
			}
			if cmplx.Abs(acc-want) > 1e-12 {
				t.Fatalf("invert: a·a⁻¹[%d][%d] = %v", i, j, acc)
			}
		}
	}
	if solveInPlace([]complex128{1, 2, 2, 4}, 2, []complex128{1, 1}) {
		t.Fatal("solve: singular matrix accepted")
	}
}
