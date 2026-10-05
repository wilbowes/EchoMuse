package outchain

// Band layout, from em_eq: a low shelf, six peaking bands, a high shelf.
var eqFrequencies = [NumBands]float64{125, 250, 500, 1000, 2000, 3500, 5500, 8000}

// NumBands is the number of EQ faders.
const NumBands = 8

const peakQ = 1.4

// eq is the 8-band graphic EQ plus the optional presence ("loudness") boost.
type eq struct {
	fs       float64
	sections []biquad // nil when flat: a pure passthrough
}

func designEQ(bands [NumBands]float64, loudness bool, fs float64) []biquad {
	out := make([]biquad, 0, NumBands+1)
	for i, fc := range eqFrequencies {
		switch i {
		case 0:
			out = append(out, lowShelf(fc, bands[i], fs))
		case NumBands - 1:
			out = append(out, highShelf(fc, bands[i], fs))
		default:
			out = append(out, peaking(fc, bands[i], peakQ, fs))
		}
	}
	if loudness {
		out = append(out, peaking(2500, 5.0, 0.8, fs))
	}
	return out
}

func isFlat(bands [NumBands]float64, loudness bool) bool {
	if loudness {
		return false
	}
	for _, b := range bands {
		if b != 0 {
			return false
		}
	}
	return true
}

// set changes the curve mid-stream, as StreamingEQ.set_bands does: state is
// carried when the section count is unchanged, and starts from zero when the
// count changes or the EQ was flat, because then there was no matching filter
// running to carry.
func (e *eq) set(bands [NumBands]float64, loudness bool) {
	if isFlat(bands, loudness) {
		e.sections = nil
		return
	}
	next := designEQ(bands, loudness, e.fs)
	if len(e.sections) != len(next) {
		e.sections = next
		return
	}
	for i := range next {
		e.sections[i].setCoeffs(next[i])
	}
}

func (e *eq) step(x float64) float64 {
	for i := range e.sections {
		x = e.sections[i].step(x)
	}
	return x
}

func (e *eq) reset() {
	for i := range e.sections {
		e.sections[i].reset()
	}
}
