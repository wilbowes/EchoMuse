package outchain

import (
	"github.com/wilbowes/EchoMuse/pkg/board"

	"compress/gzip"
	"encoding/binary"
	"io"
	"math"
	"os"
	"path/filepath"
	"testing"
)

// The voice plane's filter matches em_eq.voice_sos sample for sample, on
// both channels, and Reset starts it cold again.
func TestVoiceFilterMatchesController(t *testing.T) {
	in := readS16(t, filepath.Join("testdata", "voice_filter.in.s16.gz"))
	f, err := os.Open(filepath.Join("testdata", "voice_filter.out.f64.gz"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := io.ReadAll(gz)
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) != len(in)*8 {
		t.Fatalf("%d outputs for %d inputs", len(raw)/8, len(in))
	}
	v := NewVoiceFilter(48000, board.BiscuitVoicing())
	for pass := 0; pass < 2; pass++ {
		for ch := 0; ch < 2; ch++ {
			for i, x := range in {
				want := math.Float64frombits(binary.LittleEndian.Uint64(raw[i*8:]))
				if got := v.Step(ch, float64(x)); got != want {
					t.Fatalf("pass %d ch %d sample %d: %v, controller %v", pass, ch, i, got, want)
				}
			}
		}
		v.Reset()
	}
}
