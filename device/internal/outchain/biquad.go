// Package outchain is the speaker output chain: EQ, bass guard and peak
// limiter, run on the device after the voice/music mix and before the DAC.
//
// It is a port of the controller's em_eq / em_mbc / em_limiter, and those
// remain the reference: testdata/ holds vectors generated from the Python,
// and the tests hold this package to them sample for sample. A behaviour
// change belongs in both halves, with the vectors regenerated.
//
// Why on the device at all: the device holds up to ~5.5s of audio already
// queued, so an EQ change made on the controller cannot reach anything the
// speaker is about to play. Applied here, at the ALSA write, a change is
// audible within one period. The same argument put ducking here (mix.go).
//
// No build tag and no tinyalsa import, so all of it is tested on the host.
package outchain

import "math"

// biquad is one second-order section, Direct Form II transposed — the form
// scipy.signal.sosfilt uses, so rounding follows the reference as closely as
// float64 allows. Coefficients are normalised (a0 == 1).
type biquad struct {
	b0, b1, b2, a1, a2 float64
	z1, z2             float64
}

func (q *biquad) step(x float64) float64 {
	y := q.b0*x + q.z1
	q.z1 = q.b1*x - q.a1*y + q.z2
	q.z2 = q.b2*x - q.a2*y
	return y
}

// setCoeffs replaces the coefficients and KEEPS the state, for em_eq's
// reason: zeroing it mid-stream is a transient at the moment of the change.
func (q *biquad) setCoeffs(c biquad) {
	q.b0, q.b1, q.b2, q.a1, q.a2 = c.b0, c.b1, c.b2, c.a1, c.a2
}

func (q *biquad) reset() { q.z1, q.z2 = 0, 0 }

// dbToGain is 10^(db/20) through Exp, several times cheaper than Pow on the
// device's 32-bit ARM, and it runs per sample in the guard and the limiter.
func dbToGain(db float64) float64 { return math.Exp(db * (math.Ln10 / 20)) }

// gainCache remembers the last conversion. A gain pinned at the guard's floor
// or held by the limiter repeats sample after sample, and the same input gives
// the same output, so this changes nothing but the cost.
type gainCache struct{ db, lin float64 }

func (c *gainCache) of(db float64) float64 {
	if db != c.db || c.lin == 0 {
		c.db, c.lin = db, dbToGain(db)
	}
	return c.lin
}

func norm(b0, b1, b2, a0, a1, a2 float64) biquad {
	return biquad{b0: b0 / a0, b1: b1 / a0, b2: b2 / a0, a1: a1 / a0, a2: a2 / a0}
}

// Audio EQ Cookbook (Bristow-Johnson) designs, written as em_eq writes them.

func peaking(fc, gainDb, q, fs float64) biquad {
	A := math.Pow(10, gainDb/40)
	w0 := 2 * math.Pi * fc / fs
	cw := math.Cos(w0)
	alpha := math.Sin(w0) / (2 * q)
	return norm(1+alpha*A, -2*cw, 1-alpha*A, 1+alpha/A, -2*cw, 1-alpha/A)
}

func lowShelf(fc, gainDb, fs float64) biquad {
	A := math.Pow(10, gainDb/40)
	w0 := 2 * math.Pi * fc / fs
	cw := math.Cos(w0)
	sqA := math.Sqrt(A)
	alpha := math.Sin(w0) / math.Sqrt(2) // S=1
	return norm(
		A*((A+1)-(A-1)*cw+2*sqA*alpha),
		2*A*((A-1)-(A+1)*cw),
		A*((A+1)-(A-1)*cw-2*sqA*alpha),
		(A+1)+(A-1)*cw+2*sqA*alpha,
		-2*((A-1)+(A+1)*cw),
		(A+1)+(A-1)*cw-2*sqA*alpha,
	)
}

func highShelf(fc, gainDb, fs float64) biquad {
	A := math.Pow(10, gainDb/40)
	w0 := 2 * math.Pi * fc / fs
	cw := math.Cos(w0)
	sqA := math.Sqrt(A)
	alpha := math.Sin(w0) / math.Sqrt(2) // S=1
	return norm(
		A*((A+1)+(A-1)*cw+2*sqA*alpha),
		-2*A*((A-1)+(A+1)*cw),
		A*((A+1)+(A-1)*cw-2*sqA*alpha),
		(A+1)-(A-1)*cw+2*sqA*alpha,
		2*((A-1)-(A+1)*cw),
		(A+1)-(A-1)*cw-2*sqA*alpha,
	)
}

// butter2 is scipy's butter(2, fc/(fs/2), btype, output="sos"): a bilinear
// transform with the cutoff prewarped, which for one section reduces to the
// closed form below. The Nyquist clamp matches em_mbc._lr4.
func butter2(fc, fs float64, high bool) biquad {
	wn := math.Min(fc/(fs*0.5), 0.99)
	k := math.Tan(math.Pi * wn / 2)
	k2 := k * k
	a0 := 1 + math.Sqrt2*k + k2
	a1 := 2 * (k2 - 1)
	a2 := 1 - math.Sqrt2*k + k2
	if high {
		return norm(1, -2, 1, a0, a1, a2)
	}
	return norm(k2, 2*k2, k2, a0, a1, a2)
}
