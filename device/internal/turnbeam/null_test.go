package turnbeam

import (
	"math"
	"math/rand"
	"testing"
)

// A steady source from one direction (the TV) and a 1s burst from another
// (the talker), as integer-sample delays across four mics. The null must take
// the TV well down once it has learnt it, and keep the talker.
func TestNullRemovesTheSteadySourceAndKeepsTheTalker(t *testing.T) {
	const n = 2560 * 88 // ~14 s in whole 160ms batches
	rng := rand.New(rand.NewSource(1))
	tv := make([]float64, n+8)
	talk := make([]float64, n+8)
	for i := range tv {
		tv[i] = rng.NormFloat64() * 3000
	}
	burstAt, burstN := 16000*12, 16000
	for i := burstAt; i < burstAt+burstN; i++ {
		talk[i] = rng.NormFloat64() * 1500 // 6 dB under the TV at the mics
	}
	tvDelay := []int{0, 1, 2, 3}
	talkDelay := []int{3, 1, 0, 2}
	build := func(withTV, withTalk bool) [][]int16 {
		mics := make([][]int16, 4)
		for c := range mics {
			mics[c] = make([]int16, n)
			for i := 0; i < n; i++ {
				v := 0.0
				if withTV {
					v += tv[i+8-tvDelay[c]]
				}
				if withTalk {
					v += talk[i+8-talkDelay[c]]
				}
				mics[c][i] = int16(math.Max(-32768, math.Min(32767, v)))
			}
		}
		return mics
	}
	run := func(mics [][]int16) []int16 {
		s := New(Config{Mics: 4})
		s.EnableNull()
		var out []int16
		for off := 0; off < n; off += 2560 { // 160ms batches, as the capture delivers
			s.Push([][]int16{mics[0][off : off+2560], mics[1][off : off+2560], mics[2][off : off+2560], mics[3][off : off+2560]})
			out = append(out, s.TakeNull()...)
		}
		return out
	}
	pow := func(x []int16, from, to int) float64 {
		e := 0.0
		for _, v := range x[from:to] {
			e += float64(v) * float64(v)
		}
		return 10 * math.Log10(e/float64(to-from)+1e-9)
	}
	all := build(true, true)
	out := run(all)
	if len(out) < n-512 {
		t.Fatalf("null gave %d samples for %d in", len(out), n)
	}
	lag := hopH
	// TV alone, 10-12 s: how far down the null takes it.
	tvIn := pow(all[0], 16000*10, 16000*12)
	tvOut := pow(out, 16000*10+lag, 16000*12+lag) - 20*math.Log10(nullGain)
	if att := tvIn - tvOut; att < 15 {
		t.Errorf("TV down %.1f dB, want at least 15", att)
	}
	// Talker: the same run with the talker alone through the same filter is
	// not separable, so compare the burst's level over the TV-only floor.
	burst := pow(out, burstAt+lag+1600, burstAt+burstN+lag-1600)
	floor := pow(out, 16000*10+lag, 16000*12+lag)
	inRatio := pow(all[0], burstAt+1600, burstAt+burstN-1600) - tvIn
	if gain := (burst - floor) - inRatio; gain < 6 {
		t.Errorf("talker over TV improved %.1f dB (in %.1f, out %.1f), want at least 6", gain, inRatio, burst-floor)
	}
}

func TestNullOffGivesNothing(t *testing.T) {
	s := New(Config{Mics: 2})
	s.Push([][]int16{make([]int16, 2560), make([]int16, 2560)})
	if s.TakeNull() != nil {
		t.Fatal("null output with the null off")
	}
}
