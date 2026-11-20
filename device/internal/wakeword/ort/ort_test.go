package ort_test

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
	"github.com/wilbowes/EchoMuse/internal/wakeword/fixture"
	"github.com/wilbowes/EchoMuse/internal/wakeword/ort"
)

// These tests need the ONNX Runtime library and the three model files, none of
// which live in this repository — the library is 12MB and is distributed to
// devices separately, and the models come from openwakeword. So they SKIP
// unless pointed at them:
//
//	EM_ORT_LIB=/usr/local/lib/python3.10/dist-packages/onnxruntime/capi/libonnxruntime.so.1.23.2 \
//	EM_OWW_MODELS=/usr/local/lib/python3.10/dist-packages/openwakeword/resources/models \
//	EM_OWW_CLASSIFIER=/path/to/hey_mycroft_v0.1.onnx \
//	go test ./internal/wakeword/ort/
//
// Skipping rather than failing is deliberate: CI would otherwise need a 12MB
// download to test a package whose whole design point is that it degrades
// gracefully when the library is absent. TestOpenMissingLibraryFails covers
// that path, and it needs nothing.
func models(t *testing.T) (lib string, m ort.Models) {
	t.Helper()
	lib = os.Getenv("EM_ORT_LIB")
	dir := os.Getenv("EM_OWW_MODELS")
	cls := os.Getenv("EM_OWW_CLASSIFIER")
	if lib == "" || dir == "" || cls == "" {
		t.Skip("set EM_ORT_LIB, EM_OWW_MODELS and EM_OWW_CLASSIFIER to run the ONNX Runtime tests")
	}
	// EM_OWW_STREAM is the streaming embedding model, made from the stock
	// one by controller/tools/oww_stream.py. Without it the streaming tests
	// skip and the rest run on the stock model.
	return lib, ort.Models{
		Melspec:         filepath.Join(dir, "melspectrogram.onnx"),
		Embedding:       filepath.Join(dir, "embedding_model.onnx"),
		Classifier:      cls,
		EmbeddingStream: os.Getenv("EM_OWW_STREAM"),
	}
}

func loadFixture(t *testing.T) *fixture.ORT {
	t.Helper()
	fx, err := fixture.LoadORT("../testdata/ort_fixture.bin")
	if err != nil {
		t.Fatalf("load fixture: %v", err)
	}
	return fx
}

func newInferer(t *testing.T) *ort.Inferer {
	t.Helper()
	lib, m := models(t)
	rt, err := ort.Open(lib)
	if err != nil {
		t.Fatalf("Open(%s): %v", lib, err)
	}
	t.Logf("onnxruntime %s", rt.Version())
	inf, err := rt.NewInferer(m, ort.DefaultOptions())
	if err != nil {
		t.Fatalf("NewInferer: %v", err)
	}
	t.Cleanup(func() { inf.Close() })
	t.Logf("xnnpack active: %v, streaming embedding: %s", inf.XNNPACKActive(), inf.StreamStatus())
	return inf
}

func newStreamingInferer(t *testing.T) *ort.Inferer {
	t.Helper()
	inf := newInferer(t)
	if os.Getenv("EM_OWW_STREAM") == "" {
		t.Skip("set EM_OWW_STREAM to run the streaming embedding tests")
	}
	if !inf.Streaming() {
		t.Fatalf("streaming embedding model did not come up: %s", inf.StreamStatus())
	}
	return inf
}

// TestInfererMatchesPython is the end-to-end claim: the Go pipeline driving
// real ONNX Runtime reproduces what openWakeWord's Python produced from the
// same audio, at every stage.
//
// The parent package's fixture proves the BUFFERING by replaying model
// outputs; this proves the INFERENCE by running the models for real. Neither
// alone is sufficient — replayed models cannot catch a wrong tensor shape or a
// misconfigured session, and hand-fed models cannot catch a misaligned window.
func TestInfererMatchesPython(t *testing.T) {
	fx := loadFixture(t)
	checkAgainstPython(t, fx, fixture.Verify(fixture.StockOnly(newInferer(t)), fx), false)
}

// TestStreamingInfererMatchesPython is the same claim for the streaming
// embedding (#893): same audio, same fixture, same tolerances. It is held to
// Python and not to the stock model, so the two cannot drift together.
func TestStreamingInfererMatchesPython(t *testing.T) {
	fx := loadFixture(t)
	checkAgainstPython(t, fx, fixture.Verify(newStreamingInferer(t), fx), true)
}

func checkAgainstPython(t *testing.T, fx *fixture.ORT, rep fixture.Report, streaming bool) {
	t.Helper()
	if rep.Streaming != streaming {
		t.Fatalf("embeddings came from the streaming model: %v, want %v", rep.Streaming, streaming)
	}

	if rep.Err != nil {
		t.Fatalf("verify: %v", rep.Err)
	}
	for _, msg := range rep.Structural {
		t.Error(msg)
	}
	// Melspectrogram output is the stage most sensitive to a wrong input
	// length, and embeddings must agree from chunk 0 because the mel ring's
	// 1.0 pre-fill is deterministic. Scores only from ScoreFrom: before that
	// Python's feature ring still holds embeddings of its random warm-up audio
	// while ours is clean.
	for i := range fx.Records {
		if d := rep.Melspec[i]; !d.Ok() {
			t.Errorf("melspec chunk %d: %d values differ, worst %.3g at %d: %v",
				i, d.N, d.Worst, d.WorstAt, d.Examples)
		}
		if d := rep.Embedding[i]; !d.Ok() {
			t.Errorf("embedding chunk %d: %d values differ, worst %.3g: %v",
				i, d.N, d.Worst, d.Examples)
		}
		if i >= rep.ScoreFrom {
			if d := rep.Score[i]; !d.Ok() {
				t.Errorf("score chunk %d: %v", i, d.Examples)
			}
		}
	}
	if !rep.Ok() {
		t.Logf("report:\n%s", rep.Summary())
	}
}

// TestProbeTensorsMatchPython covers the two model stages the audio path
// exercises weakly. On synthetic noise every score is 0.0000, so a classifier
// wired to return a constant zero would pass the test above; the probe feeds a
// wide-range tensor that produces a small but distinctly non-zero score.
func TestProbeTensorsMatchPython(t *testing.T) {
	fx := loadFixture(t)
	inf := newInferer(t)

	emb, err := inf.Embed(fx.EmbProbeIn)
	if err != nil {
		t.Fatalf("Embed: %v", err)
	}
	if d := fixture.Compare(emb, fx.EmbProbeOut); !d.Ok() {
		t.Errorf("embedding probe: %d values differ, worst %.3g: %v", d.N, d.Worst, d.Examples)
	}

	score, err := inf.Classify(fx.ClsProbeIn)
	if err != nil {
		t.Fatalf("Classify: %v", err)
	}
	if fx.ClsProbeOut == 0 {
		t.Fatal("fixture probe score is zero, which would make this test vacuous")
	}
	if !fixture.CloseEnough(score, fx.ClsProbeOut) {
		t.Errorf("classifier probe = %v, Python gave %v", score, fx.ClsProbeOut)
	}
}

// TestRejectsWrongTensorSizes guards the shape checks. ONNX Runtime would
// otherwise be handed a tensor whose declared shape disagrees with its data
// length, which reads out of bounds rather than failing cleanly.
func TestRejectsWrongTensorSizes(t *testing.T) {
	inf := newInferer(t)
	if _, err := inf.Embed(make([]float32, wakeword.MelWindow*wakeword.MelBins-1)); err == nil {
		t.Error("Embed accepted a short window")
	}
	if _, err := inf.Classify(make([]float32, wakeword.FeatWindow*wakeword.FeatDim+1)); err == nil {
		t.Error("Classify accepted an oversized feature tensor")
	}
	if _, _, err := inf.Melspec(nil); err == nil {
		t.Error("Melspec accepted no samples")
	}
}

// TestUseAfterCloseFails pins that a closed Inferer reports an error rather
// than calling into a released session.
func TestUseAfterCloseFails(t *testing.T) {
	lib, m := models(t)
	rt, err := ort.Open(lib)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	inf, err := rt.NewInferer(m, ort.DefaultOptions())
	if err != nil {
		t.Fatalf("NewInferer: %v", err)
	}
	if err := inf.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	if err := inf.Close(); err != nil {
		t.Fatalf("second Close: %v", err)
	}
	if _, err := inf.Embed(make([]float32, wakeword.MelWindow*wakeword.MelBins)); !errors.Is(err, ort.ErrClosed) {
		t.Errorf("Embed after Close returned %v, want ErrClosed", err)
	}
	if _, err := inf.Classify(make([]float32, wakeword.FeatWindow*wakeword.FeatDim)); !errors.Is(err, ort.ErrClosed) {
		t.Errorf("Classify after Close returned %v, want ErrClosed", err)
	}
}

// TestOpenMissingLibraryFails is the one test here that needs no ONNX Runtime,
// and it covers the property the whole dlopen design exists for: a device with
// no libonnxruntime.so must get a clean error, not a link failure at exec and
// not a crash. The firmware keeps running with controller-side wake word.
func TestOpenMissingLibraryFails(t *testing.T) {
	_, err := ort.Open(filepath.Join(t.TempDir(), "libonnxruntime.so"))
	if err == nil {
		t.Fatal("Open succeeded for a nonexistent library")
	}
	if !strings.Contains(err.Error(), "ort: open") {
		t.Errorf("error %q does not identify itself as an ort open failure", err)
	}
}

// TestThreadsMustBePositive covers the one Options value that cannot be
// defaulted silently: ORT treats 0 as "pick for me", which would quietly
// undo the single-thread choice that keeps CPU at 36% instead of 63%.
func TestThreadsMustBePositive(t *testing.T) {
	lib, m := models(t)
	rt, err := ort.Open(lib)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	o := ort.DefaultOptions()
	o.Threads = 0
	if _, err := rt.NewInferer(m, o); err == nil {
		t.Error("NewInferer accepted Threads=0")
	}
}

// TestStreamStepsEqualStockWindows compares the two embedding models directly,
// over more audio than the fixture holds and with a reset in the middle.
func TestStreamStepsEqualStockWindows(t *testing.T) {
	inf := newStreamingInferer(t)
	const step, win = wakeword.MelStep * wakeword.MelBins, wakeword.MelWindow * wakeword.MelBins
	mel := make([]float32, 200*step)
	seed := uint64(7)
	for i := range mel {
		seed = seed*6364136223846793005 + 1442695040888963407
		mel[i] = float32((seed>>33)%2000000)/1000000*8 - 8
	}
	for _, from := range []int{0, 37} { // two starts: the state must not survive a reset
		inf.ResetStream()
		for j := from; j < 200; j++ {
			got, err := inf.EmbedStep(mel[j*step : (j+1)*step])
			if err != nil {
				t.Fatalf("step %d: %v", j, err)
			}
			end := (j + 1) * step
			if end-win < from*step {
				continue // the window reaches back before this start
			}
			got = append([]float32(nil), got...)
			want, err := inf.Embed(mel[end-win : end])
			if err != nil {
				t.Fatal(err)
			}
			if d := fixture.Compare(got, want); !d.Ok() {
				t.Fatalf("start %d step %d: %d values differ, worst %.3g: %v", from, j, d.N, d.Worst, d.Examples)
			}
		}
	}
}

// TestAStreamModelThatIsNotOneIsRefused: any ONNX file can be put at the
// streaming model's path. The stock embedding model is the likeliest wrong
// file, and it must leave the Inferer working on the stock path.
func TestAStreamModelThatIsNotOneIsRefused(t *testing.T) {
	lib, m := models(t)
	rt, err := ort.Open(lib)
	if err != nil {
		t.Fatal(err)
	}
	for _, wrong := range []string{m.Embedding, m.Classifier, filepath.Join(t.TempDir(), "absent.onnx")} {
		m.EmbeddingStream = wrong
		inf, err := rt.NewInferer(m, ort.DefaultOptions())
		if err != nil {
			t.Fatalf("a bad streaming model failed the Inferer: %v", err)
		}
		if inf.Streaming() {
			t.Errorf("%s was accepted as a streaming model", filepath.Base(wrong))
		}
		if st := inf.StreamStatus(); st == "" || st == "on" {
			t.Errorf("%s: status %q does not say why", filepath.Base(wrong), st)
		}
		if _, err := inf.EmbedStep(make([]float32, wakeword.MelStep*wakeword.MelBins)); err == nil {
			t.Errorf("EmbedStep worked with no streaming model")
		}
		inf.Close()
	}
}

// TestAWrongStreamModelFailsTheSelfCheck: a streaming model built from some
// other embedding model has the right inputs and runs. Only comparing it with
// the stock model can tell, which is what the self-check at load is for.
// EM_OWW_STREAM_WRONG is one made with `oww_stream.py --perturb`.
func TestAWrongStreamModelFailsTheSelfCheck(t *testing.T) {
	lib, m := models(t)
	wrong := os.Getenv("EM_OWW_STREAM_WRONG")
	if wrong == "" {
		t.Skip("set EM_OWW_STREAM_WRONG to a perturbed streaming model")
	}
	rt, err := ort.Open(lib)
	if err != nil {
		t.Fatal(err)
	}
	m.EmbeddingStream = wrong
	inf, err := rt.NewInferer(m, ort.DefaultOptions())
	if err != nil {
		t.Fatalf("a wrong streaming model failed the Inferer: %v", err)
	}
	defer inf.Close()
	if inf.Streaming() {
		t.Fatal("a streaming model that disagrees with the stock one was accepted")
	}
	if st := inf.StreamStatus(); !strings.HasPrefix(st, "self-check:") {
		t.Fatalf("status %q, want the self-check's reason", st)
	}
	// And the stock path still reproduces Python.
	if rep := fixture.Verify(inf, loadFixture(t)); !rep.Ok() || rep.Streaming {
		t.Fatalf("stock path after a refused streaming model: ok=%v streaming=%v", rep.Ok(), rep.Streaming)
	}
}

// TestInferersShareSessions: a scorer per microphone opens an Inferer each on
// the same files, and they share the ORT sessions. Each must still reproduce
// Python while the others run on other goroutines, and closing one must leave
// the rest working.
func TestInferersShareSessions(t *testing.T) {
	lib, m := models(t)
	rt, err := ort.Open(lib)
	if err != nil {
		t.Fatal(err)
	}
	fx := loadFixture(t)
	infs := make([]*ort.Inferer, 3)
	for k := range infs {
		if infs[k], err = rt.NewInferer(m, ort.DefaultOptions()); err != nil {
			t.Fatalf("NewInferer %d: %v", k, err)
		}
	}
	errs := make(chan error, len(infs))
	for k, inf := range infs {
		go func(k int, inf *ort.Inferer) {
			if rep := fixture.Verify(inf, fx); !rep.Ok() {
				errs <- fmt.Errorf("inferer %d diverged from Python while sharing", k)
				return
			}
			errs <- nil
		}(k, inf)
	}
	for range infs {
		if err := <-errs; err != nil {
			t.Error(err)
		}
	}
	infs[0].Close()
	infs[1].Close()
	if rep := fixture.Verify(infs[2], fx); !rep.Ok() {
		t.Fatal("the last inferer stopped reproducing Python after the others closed")
	}
	infs[2].Close()
	if _, err := infs[2].Classify(make([]float32, wakeword.FeatWindow*wakeword.FeatDim)); !errors.Is(err, ort.ErrClosed) {
		t.Fatalf("Classify after Close: %v, want ErrClosed", err)
	}
}
