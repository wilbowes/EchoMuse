package shadow

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
	"github.com/wilbowes/EchoMuse/internal/wakeword/melnative"
	"github.com/wilbowes/EchoMuse/internal/wakeword/ort"
)

// DefaultDir is where the ONNX Runtime library and the models live on the
// device. Overridable with EM_OWW_DIR, mainly so the same binary can be
// pointed at a scratch directory while iterating.
//
// The library is NOT part of the firmware: it is 12.3MB, it changes far less
// often than the binary, and embedding it would double both the OTA payload and
// the space taken by the A/B slots. It is installed out of band, and its
// absence is an ordinary, expected condition — see Open.
const DefaultDir = "/data/local/share/echomuse/oww"

// Dir returns the configured model directory.
func Dir() string {
	if d := os.Getenv("EM_OWW_DIR"); d != "" {
		return d
	}
	return DefaultDir
}

// ModelStem turns the controller's owwModel value into the classifier filename
// stem, mirroring em_oww_models.prediction_key EXACTLY.
//
// That value is a bare name for built-in models ("hey_mycroft_v0.1") and a file
// PATH for custom ones. Python's rule is: strip to the filename stem only when
// the value ends in ".onnx", otherwise pass it through untouched.
//
// The untouched case is the whole point, and getting it wrong is not
// hypothetical — an earlier version used filepath.Ext, which sees ".1" as the
// extension of "hey_mycroft_v0.1" and went looking for hey_mycroft_v0.onnx. A
// version suffix is not a file extension.
func ModelStem(owwModel string) string {
	name := strings.TrimSpace(owwModel)
	if !strings.HasSuffix(name, ".onnx") {
		return name
	}
	return strings.TrimSuffix(filepath.Base(name), ".onnx")
}

// Open loads ONNX Runtime and the three models for owwModel, and starts a
// Scorer.
//
// A missing library or model returns an error and is NOT a failure of the
// device: shadow mode is off until someone installs them, the caller logs it
// once and carries on with controller-side wake word. That is the whole reason
// the runtime is dlopen'd rather than linked.
func Open(owwModel string, threshold float32, onCross func(score, threshold float32, at time.Time)) (*Scorer, error) {
	dir := Dir()
	stem := ModelStem(owwModel)
	if stem == "" {
		return nil, fmt.Errorf("shadow: no wake word model configured")
	}

	models := ort.Models{
		Melspec:    filepath.Join(dir, "melspectrogram.onnx"),
		Embedding:  filepath.Join(dir, "embedding_model.onnx"),
		Classifier: filepath.Join(dir, stem+".onnx"),
	}
	// Checked up front so the log names the missing file. ORT's own error for a
	// bad path mentions neither which of the three it was nor what was expected
	// there, and this is a path people will get wrong while installing by hand.
	for what, p := range map[string]string{
		"melspectrogram model": models.Melspec,
		"embedding model":      models.Embedding,
		"classifier model":     models.Classifier,
	} {
		if _, err := os.Stat(p); err != nil {
			return nil, fmt.Errorf("shadow: %s not installed at %s", what, p)
		}
	}

	// The streaming embedding is optional (#893): without the file, or with
	// one that fails its self-check, the stock model scores as before.
	if p := filepath.Join(dir, EmbeddingStreamFile); fileExists(p) {
		models.EmbeddingStream = p
	}

	lib := filepath.Join(dir, "libonnxruntime.so")
	rt, err := ort.Open(lib)
	if err != nil {
		return nil, fmt.Errorf("shadow: %w", err)
	}
	inf, err := rt.NewInferer(models, ort.DefaultOptions())
	if err != nil {
		return nil, fmt.Errorf("shadow: %w", err)
	}

	// The melspectrogram natively on pffft when it agrees with this model
	// (melnative.Checked): it was half the wake word's CPU through ORT, 4.8ms
	// per 80ms chunk on C95 against 0.17ms (2026-11-17). Otherwise ORT's.
	var scoring wakeword.Inferer = inf
	var closer io.Closer = inf
	mel := "native"
	if h, why := melnative.Checked(inf); h != nil {
		scoring, closer = h, h
	} else {
		mel = "onnxruntime (" + why + ")"
	}

	s := NewScorer(scoring, threshold, onCross)
	s.closer = closer
	s.info = fmt.Sprintf("onnxruntime %s, model %s, xnnpack=%v, streaming embedding: %s, melspectrogram: %s",
		rt.Version(), stem, inf.XNNPACKActive(), inf.StreamStatus(), mel)
	return s, nil
}

// EmbeddingStreamFile is the streaming embedding model's name in Dir(). It
// must match em_oww_assets.STREAM_EMBEDDING on the controller, which installs
// it.
const EmbeddingStreamFile = "embedding_stream.onnx"

// StreamFileSig identifies the streaming model file as it is now: empty when
// absent, otherwise its size and modification time. The caller rebuilds the
// scorer when this changes, which is how a model installed while the device
// is running comes into use without a restart. A file that fails its
// self-check keeps its signature, so it is tried once and not on every push.
func StreamFileSig() string {
	st, err := os.Stat(filepath.Join(Dir(), EmbeddingStreamFile))
	if err != nil || st.IsDir() {
		return ""
	}
	return fmt.Sprintf("%d@%d", st.Size(), st.ModTime().UnixNano())
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && !st.IsDir()
}
