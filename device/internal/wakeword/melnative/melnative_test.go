package melnative

import (
	"testing"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
	"github.com/wilbowes/EchoMuse/internal/wakeword/fixture"
)

// TestMatchesFixture holds the native melspectrogram to the ORT model's
// output in the golden fixture, record by record, under fixture's policy.
func TestMatchesFixture(t *testing.T) {
	fx, err := fixture.LoadORT("../testdata/ort_fixture.bin")
	if err != nil {
		t.Fatal(err)
	}
	m, err := New()
	if err != nil {
		t.Fatal(err)
	}
	defer m.Close()
	var worstRel float64
	for i, rec := range fx.Records {
		end := (i + 1) * wakeword.ChunkSamples
		src := fx.Audio[end-rec.MelInLen : end]
		in := make([]float32, len(src))
		for k, s := range src {
			in[k] = float32(s)
		}
		got, frames, err := m.Melspec(in)
		if err != nil {
			t.Fatalf("record %d: %v", i, err)
		}
		if frames*wakeword.MelBins != len(rec.MelOut) {
			t.Fatalf("record %d: %d frames, fixture has %d", i, frames, len(rec.MelOut)/wakeword.MelBins)
		}
		d := fixture.Compare(got, rec.MelOut)
		if !d.Ok() {
			t.Errorf("record %d: %d values outside tolerance, worst %.3g (tol %.3g): %v", i, d.N, d.Worst, d.Tol, d.Examples)
		}
		if r := d.RelToScale(); r > worstRel {
			worstRel = r
		}
	}
	t.Logf("%d records, worst difference %.3g of scale (policy %.0e)", len(fx.Records), worstRel, fixture.RelTol)
}

func BenchmarkMelspec(b *testing.B) {
	m, _ := New()
	defer m.Close()
	in := make([]float32, 1760)
	for i := range in {
		in[i] = float32((i*7919)%20001 - 10000)
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		m.Melspec(in)
	}
}

// fakeModel stands in for ORT's melspectrogram on the host: this package's
// own output, scaled by k.
type fakeModel struct {
	wakeword.Inferer
	k float32
}

func (f fakeModel) Melspec(in []float32) ([]float32, int, error) {
	m, err := New()
	if err != nil {
		return nil, 0, err
	}
	defer m.Close()
	out, n, err := m.Melspec(in)
	cp := make([]float32, len(out))
	for i, v := range out {
		cp[i] = v * f.k
	}
	return cp, n, err
}

// Checked is the gate between a model on the device and this arithmetic:
// one that agrees is taken, one that does not (a different melspectrogram
// model installed) keeps the model's own, and says why.
func TestCheckedTakesOnlyAModelThatAgrees(t *testing.T) {
	if h, why := Checked(fakeModel{k: 1}); h == nil {
		t.Fatalf("an agreeing model was refused: %s", why)
	} else {
		h.mel.Close()
	}
	if h, why := Checked(fakeModel{k: 1.01}); h != nil || why == "" {
		t.Fatal("a model 1% off was taken")
	}
}
