package turnbeam

import "testing"

func benchStage(b *testing.B, iva bool) {
	s := New(Config{Mics: 4})
	if iva {
		s.EnableIVA()
	}
	in := make([][]int16, 4)
	for c := range in {
		in[c] = make([]int16, 2560)
		for i := range in[c] {
			in[c][i] = int16((i*31 + c*7) % 2000)
		}
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		s.Push(in)
		s.TakeIVA()
	}
}

// One 160ms batch (10 hops) per op: ns/op ÷ 160e6 is the share of a core.
func BenchmarkStage(b *testing.B)        { benchStage(b, false) }
func BenchmarkStageWithIVA(b *testing.B) { benchStage(b, true) }
