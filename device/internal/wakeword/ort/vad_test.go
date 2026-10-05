package ort_test

import (
	"encoding/binary"
	"encoding/json"
	"math"
	"os"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/wakeword/ort"
)

// Needs the runtime and openwakeword's silero_vad.onnx, so it skips without:
//
//	EM_ORT_LIB=... EM_VAD_MODEL=.../openwakeword/resources/models/silero_vad.onnx
func TestVADMatchesPython(t *testing.T) {
	lib, path := os.Getenv("EM_ORT_LIB"), os.Getenv("EM_VAD_MODEL")
	if lib == "" || path == "" {
		t.Skip("set EM_ORT_LIB and EM_VAD_MODEL to run the VAD test")
	}
	rt, err := ort.Open(lib)
	if err != nil {
		t.Fatal(err)
	}
	v, err := rt.NewVAD(path, ort.DefaultOptions())
	if err != nil {
		t.Fatal(err)
	}
	defer v.Close()

	audio, want := LoadVADFixture(t, "../testdata")
	st := v.Stream()
	for i, w := range want {
		got, err := st.Prob(audio[i*1280 : (i+1)*1280])
		if err != nil {
			t.Fatal(err)
		}
		if math.Abs(float64(got-w)) > 1e-3 {
			t.Errorf("frame %d: %.4f, Python %.4f", i, got, w)
		}
	}
	// A new stream starts from clean state and reproduces the first frame.
	if got, _ := v.Stream().Prob(audio[:1280]); math.Abs(float64(got-want[0])) > 1e-3 {
		t.Errorf("new stream: %.4f, want %.4f", got, want[0])
	}
}

// LoadVADFixture reads testdata/vad_fixture.{pcm,json} as float samples and
// Python's per-frame probabilities.
func LoadVADFixture(t *testing.T, dir string) ([]float32, []float32) {
	t.Helper()
	raw, err := os.ReadFile(dir + "/vad_fixture.pcm")
	if err != nil {
		t.Fatal(err)
	}
	audio := make([]float32, len(raw)/2)
	for i := range audio {
		audio[i] = float32(int16(binary.LittleEndian.Uint16(raw[2*i:]))) / 32767
	}
	var g struct{ Probs []float32 }
	js, err := os.ReadFile(dir + "/vad_fixture.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(js, &g); err != nil {
		t.Fatal(err)
	}
	return audio, g.Probs
}
