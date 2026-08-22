// Package outchain is the device-side output chain: EQ, bass guard and
// limiter, applied to the mixed audio on its way to the speaker.
//
// It is a port of the controller's `em_eq.py` / `em_limiter.py` / `em_mbc.py`,
// moved onto the device for 3.0.0 (docs/audio-states.md section 8, #243).
// Correctness is defined by agreement with that reference, captured in
// internal/outchain/testdata and checked by internal/outchain/fixture — never
// by inspection, because "sounds about right" is exactly the standard this
// move has to beat.
//
// No build tag: this is arithmetic, and the whole point is that it runs in the
// host test suite. Nothing here touches ALSA.
package outchain

import "math"

// EQFrequencies and the band types are fixed by the reference implementation
// and by every stored config that names eight bands. Band 0 is a low shelf,
// band 7 a high shelf, the rest peaking at Q=1.4 (~1 octave).
var EQFrequencies = [...]float64{125, 250, 500, 1000, 2000, 3500, 5500, 8000}

// NumBands is the length every `eqBands` config value is padded or truncated
// to, matching em_eq.NUM_BANDS.
const NumBands = len(EQFrequencies)

const peakQ = 1.4

// Loudness is a fixed presence boost appended as a NINTH section when
// enabled — em_eq._loudness_sos: peaking, 2500Hz, +5dB, Q=0.8.
const (
	loudnessFreq   = 2500.0
	loudnessGainDB = 5.0
	loudnessQ      = 0.8
)

// biquad is one second-order section in transposed direct form II — the same
// form scipy's sosfilt uses, which is not an implementation detail we are free
// to choose: the state variables of TDF-II are not the state variables of
// DF-I, so a different form carrying "the same" state across a coefficient
// change would produce a different transient. Matching the reference at a
// parameter change is the whole point of section 8.3.
//
// Coefficients are stored already normalised by a0, as the reference's SOS
// rows are.
type biquad struct {
	b0, b1, b2 float64
	a1, a2     float64
	z0, z1     float64 // state, carried across chunks AND across coefficient changes
}

// process filters in place. Kept as a method on one section rather than a loop
// over a matrix so the state lives with the coefficients it belongs to.
func (s *biquad) process(x []float64) {
	b0, b1, b2, a1, a2 := s.b0, s.b1, s.b2, s.a1, s.a2
	z0, z1 := s.z0, s.z1
	for i, in := range x {
		y := b0*in + z0
		z0 = b1*in - a1*y + z1
		z1 = b2*in - a2*y
		x[i] = y
	}
	s.z0, s.z1 = z0, z1
}

// setCoeffs replaces the coefficients and DELIBERATELY LEAVES THE STATE.
// Zeroing it would produce a transient at the moment of the change — precisely
// when someone is listening for the difference the change made. See
// em_eq.set_bands, which says the same thing and is the behaviour the fixture
// captures.
func (s *biquad) setCoeffs(b0, b1, b2, a1, a2 float64) {
	s.b0, s.b1, s.b2, s.a1, s.a2 = b0, b1, b2, a1, a2
}

// ─── Filter design (Audio EQ Cookbook, Robert Bristow-Johnson) ───────────────
//
// Ported coefficient-for-coefficient from em_eq. The expressions are kept in
// the reference's shape rather than simplified: an algebraically equivalent
// rearrangement is not bit-equivalent in floating point, and the fixture
// compares against what Python actually computed.

func peakCoeffs(fc, gainDB, q, fs float64) (b0, b1, b2, a1, a2 float64) {
	A := math.Pow(10, gainDB/40.0)
	w0 := 2 * math.Pi * fc / fs
	cw := math.Cos(w0)
	alpha := math.Sin(w0) / (2 * q)
	nb0 := 1 + alpha*A
	nb1 := -2 * cw
	nb2 := 1 - alpha*A
	na0 := 1 + alpha/A
	na1 := -2 * cw
	na2 := 1 - alpha/A
	return nb0 / na0, nb1 / na0, nb2 / na0, na1 / na0, na2 / na0
}

func loShelfCoeffs(fc, gainDB, fs float64) (b0, b1, b2, a1, a2 float64) {
	A := math.Pow(10, gainDB/40.0)
	w0 := 2 * math.Pi * fc / fs
	cw := math.Cos(w0)
	sqA := math.Sqrt(A)
	alpha := math.Sin(w0) / math.Sqrt2 // S=1
	nb0 := A * ((A + 1) - (A-1)*cw + 2*sqA*alpha)
	nb1 := 2 * A * ((A - 1) - (A+1)*cw)
	nb2 := A * ((A + 1) - (A-1)*cw - 2*sqA*alpha)
	na0 := (A + 1) + (A-1)*cw + 2*sqA*alpha
	na1 := -2 * ((A - 1) + (A+1)*cw)
	na2 := (A + 1) + (A-1)*cw - 2*sqA*alpha
	return nb0 / na0, nb1 / na0, nb2 / na0, na1 / na0, na2 / na0
}

func hiShelfCoeffs(fc, gainDB, fs float64) (b0, b1, b2, a1, a2 float64) {
	A := math.Pow(10, gainDB/40.0)
	w0 := 2 * math.Pi * fc / fs
	cw := math.Cos(w0)
	sqA := math.Sqrt(A)
	alpha := math.Sin(w0) / math.Sqrt2 // S=1
	nb0 := A * ((A + 1) + (A-1)*cw + 2*sqA*alpha)
	nb1 := -2 * A * ((A - 1) + (A+1)*cw)
	nb2 := A * ((A + 1) + (A-1)*cw - 2*sqA*alpha)
	na0 := (A + 1) - (A-1)*cw + 2*sqA*alpha
	na1 := 2 * ((A - 1) - (A+1)*cw)
	na2 := (A + 1) - (A-1)*cw - 2*sqA*alpha
	return nb0 / na0, nb1 / na0, nb2 / na0, na1 / na0, na2 / na0
}

// ─── EQ ──────────────────────────────────────────────────────────────────────

// EQ is the eight-band equaliser, plus the optional loudness section.
//
// Float64 throughout, matching the reference. That is a starting point, not a
// conclusion: R7 in section 8.2 says fixed-point-versus-float is to be
// measured rather than assumed, and this is the version that makes the
// measurement meaningful, because it is the one that can be shown to agree
// with Python first. Nine biquads at 48kHz is ~4 Mflop/s, which is unlikely to
// be the reason to reach for anything narrower.
type EQ struct {
	fs       float64
	sections []biquad

	// flat is the reference's `_sos is None` case: all bands zero and no
	// loudness means PURE PASSTHROUGH, not a chain of unity biquads. Those
	// are not the same thing in floating point, and the fixture's eq_flat
	// case is bit-exact only if this branch exists.
	flat bool
}

// NewEQ builds the chain for the given bands. bands shorter than NumBands is
// zero-padded and longer is truncated, matching the reference's tolerance for
// a config that disagrees with the code.
func NewEQ(sampleRate int, bands []float64, loudness bool) *EQ {
	e := &EQ{fs: float64(sampleRate)}
	e.SetBands(bands, loudness)
	return e
}

func normaliseBands(bands []float64) []float64 {
	out := make([]float64, NumBands)
	copy(out, bands)
	return out
}

func allZero(b []float64) bool {
	for _, v := range b {
		if v != 0 {
			return false
		}
	}
	return true
}

// SetBands changes the curve mid-stream, keeping filter state where the
// reference keeps it.
//
// THE STATE RULE IS COPIED EXACTLY, INCLUDING ITS EDGE. em_eq.set_bands keeps
// the biquad state across a coefficient change, but reallocates (and so
// zeroes) it when the section COUNT changes or when coming from flat. Toggling
// loudness changes the count 8↔9, so it is a state reset — which reads like an
// oversight and is the reference's actual behaviour, so the port reproduces it
// rather than improving on it. Diverging here would show up only as a
// different transient at one specific toggle, which is exactly the class of
// difference nobody finds by listening.
func (e *EQ) SetBands(bands []float64, loudness bool) {
	b := normaliseBands(bands)

	if !loudness && allZero(b) {
		e.flat = true
		e.sections = nil
		return
	}

	n := NumBands
	if loudness {
		n++
	}
	// Reallocate — and so drop the state — exactly when the reference does.
	if e.flat || len(e.sections) != n {
		e.sections = make([]biquad, n)
	}
	e.flat = false

	for i := 0; i < NumBands; i++ {
		var b0, b1, b2, a1, a2 float64
		switch i {
		case 0:
			b0, b1, b2, a1, a2 = loShelfCoeffs(EQFrequencies[i], b[i], e.fs)
		case NumBands - 1:
			b0, b1, b2, a1, a2 = hiShelfCoeffs(EQFrequencies[i], b[i], e.fs)
		default:
			b0, b1, b2, a1, a2 = peakCoeffs(EQFrequencies[i], b[i], peakQ, e.fs)
		}
		e.sections[i].setCoeffs(b0, b1, b2, a1, a2)
	}
	if loudness {
		b0, b1, b2, a1, a2 := peakCoeffs(loudnessFreq, loudnessGainDB, loudnessQ, e.fs)
		e.sections[NumBands].setCoeffs(b0, b1, b2, a1, a2)
	}
}

// Process filters one chunk in place, cascading the sections in order.
func (e *EQ) Process(x []float64) {
	if e.flat {
		return
	}
	for i := range e.sections {
		e.sections[i].process(x)
	}
}

// Flat reports whether the EQ is in pure-passthrough mode.
func (e *EQ) Flat() bool { return e.flat }

// Sections is the section count, for tests that care that loudness adds one.
func (e *EQ) Sections() int { return len(e.sections) }
