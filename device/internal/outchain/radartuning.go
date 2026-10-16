package outchain

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
)

// RadarTuningDir is where Radar's stock audio service keeps its playback
// tuning: AFE.cfg, which names the stages, and the files it points at.
const RadarTuningDir = "/system/vendor/etc/audio-algorithms"

// RadarTuning is Radar's stock playback tuning, read from the Echo's own
// /system at start-up. None of it is in this repository: a build distributes
// no vendor tuning, and an Echo without the files plays without the stock
// curve. What the stages DO with these numbers — the filter designs, the
// compressor and the limiter — is in radarmbc.go, stockcomp.go,
// stocklimiter.go and eqfir.go.
type RadarTuning struct {
	// The "Equalizer FIR": one filter per volume band, and each band's
	// upper volume value (AFE.cfg's "Volume Boundary").
	FIRBands  [][]float64
	FIRBounds []float64
	// ParametricEQ.cfg, without its BYPASS entries.
	PEQ []tuningBiquad
	// OutputTrim's gain.
	TrimDb float64
	// MBCL.cfg. Nil when the playback path has no MBCL.
	MBCL *mbclSpec
}

type tuningBiquad struct {
	FilterType string  `json:"FilterType"`
	Fc         float64 `json:"Fc"`
	Q          float64 `json:"Q"`
	GaindB     float64 `json:"GaindB"`
}

// mbclBand is one row of MBCL.cfg's "Bands Definition".
type mbclBand struct {
	CompInVol   float64 `json:"comp_inVol"`
	CompRatio   float64 `json:"comp_ratio"`
	CompThresh  float64 `json:"comp_thresh"`
	CompGainMin float64 `json:"comp_gainMin"`
	LimInVol    float64 `json:"lim_inVol"`
	LimThresh   float64 `json:"lim_thresh"`
	LimRelease  float64 `json:"lim_release"`
}

type mbclLimiterSpec struct {
	LimInVol   float64 `json:"lim_inVol"`
	LimThresh  float64 `json:"lim_thresh"`
	LimRelease float64 `json:"lim_release"`
}

// mbclSpec is MBCL.cfg: an input gain ("system gain"), three crossover
// frequencies, four bands and a full-band limiter.
type mbclSpec struct {
	Bypass   bool            `json:"Bypass"`
	InVol    float64         `json:"inVol"`
	FC       []float64       `json:"FilterBank FC"`
	Bands    []mbclBand      `json:"Bands Definition"`
	FullBand mbclLimiterSpec `json:"Full-band limiter"`
}

// String summarises what was loaded, for the log.
func (t *RadarTuning) String() string {
	if t == nil {
		return "none"
	}
	return fmt.Sprintf("fir=%d bands %v, peq=%d biquads, trim=%+gdB, mbcl=%v",
		len(t.FIRBands), t.FIRBounds, len(t.PEQ), t.TrimDb, t.MBCL != nil)
}

// LoadRadarTuning reads the tuning from dir. The stages are the ones AFE.cfg's
// Playback path lists, so a stage stock has commented out stays out. A stage
// that is listed but whose file is missing or unreadable is an error: part of
// the tuning is a different sound from the one the files describe, and the
// chain then plays without any of it.
func LoadRadarTuning(dir string) (*RadarTuning, error) {
	data, err := os.ReadFile(filepath.Join(dir, "AFE.cfg"))
	if err != nil {
		return nil, err
	}
	var afe struct {
		Paths struct {
			Playback struct {
				Algorithms map[string]string `json:"Algorithms"`
			} `json:"Playback"`
		} `json:"Path Definition"`
		Defs map[string]json.RawMessage `json:"Algorithm Definition"`
	}
	if err := json.Unmarshal(stripComments(data), &afe); err != nil {
		return nil, fmt.Errorf("AFE.cfg: %w", err)
	}
	stage := func(name string) json.RawMessage {
		def, ok := afe.Paths.Playback.Algorithms[name]
		if !ok {
			return nil
		}
		return afe.Defs[def]
	}

	t := &RadarTuning{}
	var errs []error
	if raw := stage("EQ"); raw != nil {
		var def struct {
			Bypass   bool      `json:"Bypass"`
			Files    []string  `json:"External Coefficients"`
			Boundary []float64 `json:"Volume Boundary"`
		}
		switch {
		case json.Unmarshal(raw, &def) != nil:
			errs = append(errs, errors.New("AFE.cfg: Equalizer FIR: unreadable"))
		case def.Bypass:
		case len(def.Files) == 0 || len(def.Files) != len(def.Boundary):
			errs = append(errs, fmt.Errorf("AFE.cfg: Equalizer FIR: %d files for %d boundaries", len(def.Files), len(def.Boundary)))
		default:
			for _, name := range def.Files {
				taps, err := readFIR(filepath.Join(dir, filepath.Base(name)))
				if err != nil {
					errs = append(errs, err)
					break
				}
				if len(t.FIRBands) > 0 && len(taps) != len(t.FIRBands[0]) {
					errs = append(errs, fmt.Errorf("%s: %d taps, the others have %d", name, len(taps), len(t.FIRBands[0])))
					break
				}
				t.FIRBands = append(t.FIRBands, taps)
			}
			t.FIRBounds = def.Boundary
		}
	}
	if raw := stage("ParametricEQ"); raw != nil {
		var def struct {
			File string `json:"External Config"`
		}
		if err := json.Unmarshal(raw, &def); err != nil || def.File == "" {
			errs = append(errs, errors.New("AFE.cfg: Parametric EQ: no config file"))
		} else if data, err := os.ReadFile(filepath.Join(dir, filepath.Base(def.File))); err != nil {
			errs = append(errs, err)
		} else if t.PEQ, err = parsePEQ(data); err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", def.File, err))
		}
	}
	if raw := stage("MBCL"); raw != nil {
		var def struct {
			Files []string `json:"External Config"`
		}
		if err := json.Unmarshal(raw, &def); err != nil || len(def.Files) != 1 {
			errs = append(errs, errors.New("AFE.cfg: MBCL: want one config file"))
		} else if data, err := os.ReadFile(filepath.Join(dir, filepath.Base(def.Files[0]))); err != nil {
			errs = append(errs, err)
		} else if t.MBCL, err = parseMBCL(data); err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", def.Files[0], err))
		}
	}
	if raw := stage("OutputTrim"); raw != nil {
		var def struct {
			GaindB float64 `json:"GaindB"`
		}
		if err := json.Unmarshal(raw, &def); err != nil {
			errs = append(errs, errors.New("AFE.cfg: OutputTrim: unreadable"))
		}
		t.TrimDb = def.GaindB
	}
	if len(errs) > 0 {
		return nil, errors.Join(errs...)
	}
	return t, nil
}

// stripComments removes /* */ and // comments outside strings, which these
// files carry around otherwise-valid JSON (stock strips them with
// cJSON_Minify).
func stripComments(data []byte) []byte {
	var out []byte
	inStr, esc := false, false
	for i := 0; i < len(data); i++ {
		c := data[i]
		if inStr {
			out = append(out, c)
			switch {
			case esc:
				esc = false
			case c == '\\':
				esc = true
			case c == '"':
				inStr = false
			}
			continue
		}
		if c == '"' {
			inStr = true
			out = append(out, c)
			continue
		}
		if c == '/' && i+1 < len(data) && data[i+1] == '/' {
			for i < len(data) && data[i] != '\n' {
				i++
			}
			out = append(out, '\n')
			continue
		}
		if c == '/' && i+1 < len(data) && data[i+1] == '*' {
			i += 2
			for i+1 < len(data) && !(data[i] == '*' && data[i+1] == '/') {
				i++
			}
			i++ // past '/'
			continue
		}
		out = append(out, c)
	}
	return out
}

var firNumber = regexp.MustCompile(`[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?`)

// readFIR reads an EQ_<n>.cfg: the taps as a comma-separated list of numbers.
func readFIR(path string) ([]float64, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var taps []float64
	for _, s := range firNumber.FindAll(stripComments(data), -1) {
		v, err := strconv.ParseFloat(string(s), 64)
		if err != nil || math.IsNaN(v) || math.IsInf(v, 0) {
			return nil, fmt.Errorf("%s: invalid tap %q", filepath.Base(path), s)
		}
		taps = append(taps, v)
	}
	if len(taps) == 0 {
		return nil, fmt.Errorf("%s: no taps", filepath.Base(path))
	}
	return taps, nil
}

// parsePEQ reads ParametricEQ.cfg. Only the two filter types stock's design
// is ported for (stockLowShelf, stockPeak) are accepted: any other would be
// a guess at a design nobody has read out of libasp.
func parsePEQ(data []byte) ([]tuningBiquad, error) {
	var f struct {
		Bypass  bool           `json:"Bypass"`
		Biquads []tuningBiquad `json:"Biquad Definitions"`
	}
	if err := json.Unmarshal(stripComments(data), &f); err != nil {
		return nil, err
	}
	if f.Bypass {
		return nil, nil
	}
	var out []tuningBiquad
	for _, b := range f.Biquads {
		switch b.FilterType {
		case "BYPASS":
			continue
		case "PEAK", "LOW_SHELF":
		default:
			return nil, fmt.Errorf("filter type %q is not ported", b.FilterType)
		}
		if b.Fc <= 0 || b.Q <= 0 {
			return nil, fmt.Errorf("%s at %gHz: invalid Fc/Q", b.FilterType, b.Fc)
		}
		out = append(out, b)
	}
	return out, nil
}

func parseMBCL(data []byte) (*mbclSpec, error) {
	var m mbclSpec
	if err := json.Unmarshal(stripComments(data), &m); err != nil {
		return nil, err
	}
	if m.Bypass {
		return nil, nil
	}
	if len(m.FC) != 3 || len(m.Bands) != 4 {
		return nil, fmt.Errorf("want 3 crossover frequencies and 4 bands, got %d and %d", len(m.FC), len(m.Bands))
	}
	for i, fc := range m.FC {
		if fc <= 0 || (i > 0 && fc <= m.FC[i-1]) {
			return nil, fmt.Errorf("crossover frequencies must rise: %v", m.FC)
		}
	}
	return &m, nil
}

// peqBiquads designs the ParametricEQ through stock's own filter designs.
func (t *RadarTuning) peqBiquads(fs float64) []biquad {
	var out []biquad
	for _, b := range t.PEQ {
		switch b.FilterType {
		case "LOW_SHELF":
			out = append(out, stockLowShelf(b.Fc, b.GaindB, fs)) // stock ignores the shelf Q
		case "PEAK":
			out = append(out, stockPeak(b.Fc, b.GaindB, b.Q, fs))
		}
	}
	return out
}
