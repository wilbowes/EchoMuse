package client

import (
	"encoding/binary"
	"fmt"
	"log"
	"os"
	"path/filepath"

	"github.com/wilbowes/EchoMuse/internal/wakeword/ort"
	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
)

// The turn stream's speech gate: whether a period is speech, for the
// no-speech timeout, end of speech and AGC's release. It was an RMS threshold,
// which anything loud enough passes — music residue under a duck, a fan, a TV
// — so a turn nobody spoke in never timed out and one that ended never closed.
// Silero separates them: on the controller's recordings real speech peaked
// 0.72-1.00 and silent turns 0.02-0.03 (em_speechgate, which applies the same
// model to what HA receives). ~7ms a period on VVV, and only while a turn is
// open.
//
// The model arrives with the wake word assets. Until it does, or if it fails,
// the RMS threshold decides exactly as before.

// sileroModel is the typed-field rewrite of openwakeword's silero_vad.onnx;
// the file as shipped crashes ORT on armv7 (see ort.NewVAD). Must match
// em_oww_assets.VAD_NAME on the controller.
const sileroModel = "silero_vad.onnx"

// speechProb is the bar a period must reach to count as speech: Silero's
// usual operating point, and the controller gate's.
const speechProb = 0.5

// speechScorer scores one stream's periods.
type speechScorer interface {
	Prob(samples []float32) (float32, error)
}

// openSilero loads the model beside the wake word assets. Cheap when it is
// absent (one stat), so a device that has not been given it yet retries per
// turn at no cost.
func openSilero() (*ort.VAD, error) {
	dir := shadow.Dir()
	model := filepath.Join(dir, sileroModel)
	if _, err := os.Stat(model); err != nil {
		return nil, fmt.Errorf("%s not installed", model)
	}
	rt, err := ort.Open(filepath.Join(dir, "libonnxruntime.so"))
	if err != nil {
		return nil, err
	}
	return rt.NewVAD(model, ort.DefaultOptions())
}

// loadSilero returns the shared session, loading it on first success.
func (d *DataClient) loadSilero() *ort.VAD {
	d.vadMu.Lock()
	defer d.vadMu.Unlock()
	if d.vad != nil {
		return d.vad
	}
	v, err := openSilero()
	if err != nil {
		if msg := err.Error(); msg != d.vadErr {
			d.vadErr = msg
			log.Printf("[data] speech gate: RMS threshold (%v)", err)
		}
		return nil
	}
	d.vad, d.vadErr = v, ""
	log.Printf("[data] speech gate: Silero VAD (xnnpack=%v)", v.XNNPACKActive())
	return v
}

// sileroStream is the default newSpeechStream: a fresh state per turn, or nil
// for the RMS threshold.
func (d *DataClient) sileroStream() speechScorer {
	if v := d.loadSilero(); v != nil {
		return v.Stream()
	}
	return nil
}

// monoFloat converts S16LE mono to -1..1, the scale Silero was trained on.
func monoFloat(mono []byte) []float32 {
	out := make([]float32, len(mono)/2)
	for i := range out {
		out[i] = float32(int16(binary.LittleEndian.Uint16(mono[2*i:]))) / 32767
	}
	return out
}
