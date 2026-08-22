// Package fixture reads the golden capture in outchain/testdata and defines
// what it means for a device-side output chain to agree with the controller's.
//
// The chain — EQ, bass guard, limiter — is moving from the controller onto the
// device (docs/audio-states.md section 8). The one risk in that move without a
// known method is whether the port SOUNDS THE SAME. This package is the
// instrument that turns that from a listening test at the end into a test
// failure in the middle, and it is deliberately the same shape as
// wakeword/fixture, for the same reason: the parse and the tolerance policy
// must not be written twice, or a second consumer will report agreement the
// first would not.
//
// The reference is `controller/em_eq.py` and friends, captured by
// testdata/gen_chain_fixture.py. Regenerate with that script; never hand-edit
// the .bin.
package fixture

import (
	"encoding/binary"
	"fmt"
	"math"
	"os"
)

// Params is one settable state of the whole chain — the arguments
// StreamingEQ.update() takes, so applying a Step is one update call.
type Params struct {
	Bands              []float32
	Loudness           bool
	LimiterEnabled     bool
	LimiterThresholdDB float32
	LimiterReleaseMS   float32
	GuardEnabled       bool
	GuardDB            float32
}

// Step is one chunk: the parameters in force, what went in, what Python
// produced. A step whose Params differ from the previous one exercises the
// in-place update path — see section 8.3 on why that is the interesting case.
type Step struct {
	Params Params
	In     []int16
	Want   []int16
}

// Case is a named sequence of steps sharing one chain instance. The chain
// carries state across steps (biquad histories, the limiter's look-ahead and
// gain envelope, the guard's detector), so steps must be replayed IN ORDER
// against ONE instance. Replaying them independently would pass while proving
// nothing about the part most likely to be wrong.
type Case struct {
	Name  string
	Steps []Step
	// HasLimiter / HasGuard say which processors were ATTACHED, which is not
	// the same as enabled. A disabled limiter still applies its 5ms
	// look-ahead delay — deliberately, so that toggling it does not shift the
	// audio and click — so a case with the limiter attached but disabled
	// still carries that latency in every sample. Only an ABSENT processor
	// isolates the stages, which is what these flags record.
	HasLimiter bool
	HasGuard   bool
	// Tail is what flush() emitted at end of stream: the limiter's held
	// look-ahead. Empty when no limiter is attached.
	Tail []int16
}

// Chain is the whole fixture.
type Chain struct {
	SampleRate int
	ChunkSize  int

	// Reference constants, carried so the port can check its own filter
	// DESIGN and its own law constants against Python directly, rather than
	// only inferring an error from audio that disagrees. Those two failures
	// look identical to a test that compares output alone and want completely
	// different investigations.
	CrossoverHz float64
	// CrossoverLow / CrossoverHigh are the Linkwitz-Riley halves as stacked
	// second-order sections, each row [b0 b1 b2 1 a1 a2] exactly as scipy
	// produced it.
	CrossoverLow  [][6]float64
	CrossoverHigh [][6]float64

	BassRatio       float64
	BassThresholdDB float64
	BassReleaseMS   float64

	LookaheadMS        float64
	ReleaseReferenceDB float64

	Cases []Case
}

const magic = "EMCHAIN3"

// Load reads and validates the fixture. Every size is read from the file's own
// headers, and the whole file must be consumed — a fixture that parses but
// leaves bytes over means the writer and reader disagree, which is worth an
// error rather than a silently truncated comparison.
func Load(path string) (*Chain, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if len(raw) < len(magic) || string(raw[:len(magic)]) != magic {
		return nil, fmt.Errorf("fixture %s: bad magic (want %s)", path, magic)
	}
	p := len(magic)

	// need reports whether n more bytes are available, so a truncated file
	// produces an error instead of a slice-bounds panic.
	need := func(n int) error {
		if p+n > len(raw) {
			return fmt.Errorf("fixture %s: truncated at %d, need %d more", path, p, n)
		}
		return nil
	}
	u32 := func() (uint32, error) {
		if err := need(4); err != nil {
			return 0, err
		}
		v := binary.LittleEndian.Uint32(raw[p:])
		p += 4
		return v, nil
	}
	u16 := func() (uint16, error) {
		if err := need(2); err != nil {
			return 0, err
		}
		v := binary.LittleEndian.Uint16(raw[p:])
		p += 2
		return v, nil
	}
	u8 := func() (byte, error) {
		if err := need(1); err != nil {
			return 0, err
		}
		v := raw[p]
		p++
		return v, nil
	}
	f32 := func() (float32, error) {
		v, err := u32()
		return math.Float32frombits(v), err
	}
	pcm := func() ([]int16, error) {
		n, err := u32()
		if err != nil {
			return nil, err
		}
		if n%2 != 0 {
			return nil, fmt.Errorf("fixture %s: odd PCM byte count %d", path, n)
		}
		if err := need(int(n)); err != nil {
			return nil, err
		}
		out := make([]int16, n/2)
		for i := range out {
			out[i] = int16(binary.LittleEndian.Uint16(raw[p+2*i:]))
		}
		p += int(n)
		return out, nil
	}

	c := &Chain{}
	sr, err := u32()
	if err != nil {
		return nil, err
	}
	ck, err := u32()
	if err != nil {
		return nil, err
	}
	c.SampleRate, c.ChunkSize = int(sr), int(ck)

	f64 := func() (float64, error) {
		if err := need(8); err != nil {
			return 0, err
		}
		v := math.Float64frombits(binary.LittleEndian.Uint64(raw[p:]))
		p += 8
		return v, nil
	}
	sosBlock := func() ([][6]float64, error) {
		n, err := u32()
		if err != nil {
			return nil, err
		}
		out := make([][6]float64, n)
		for i := range out {
			for j := 0; j < 6; j++ {
				if out[i][j], err = f64(); err != nil {
					return nil, err
				}
			}
		}
		return out, nil
	}

	if c.CrossoverHz, err = f64(); err != nil {
		return nil, err
	}
	if c.CrossoverLow, err = sosBlock(); err != nil {
		return nil, err
	}
	if c.CrossoverHigh, err = sosBlock(); err != nil {
		return nil, err
	}
	for _, dst := range []*float64{
		&c.BassRatio, &c.BassThresholdDB, &c.BassReleaseMS,
		&c.LookaheadMS, &c.ReleaseReferenceDB,
	} {
		if *dst, err = f64(); err != nil {
			return nil, err
		}
	}

	nCases, err := u32()
	if err != nil {
		return nil, err
	}
	for ci := uint32(0); ci < nCases; ci++ {
		nameLen, err := u16()
		if err != nil {
			return nil, err
		}
		if err := need(int(nameLen)); err != nil {
			return nil, err
		}
		cs := Case{Name: string(raw[p : p+int(nameLen)])}
		p += int(nameLen)

		hl, err := u8()
		if err != nil {
			return nil, err
		}
		hg, err := u8()
		if err != nil {
			return nil, err
		}
		cs.HasLimiter, cs.HasGuard = hl != 0, hg != 0

		nSteps, err := u32()
		if err != nil {
			return nil, err
		}
		for si := uint32(0); si < nSteps; si++ {
			var st Step
			nb, err := u16()
			if err != nil {
				return nil, err
			}
			st.Params.Bands = make([]float32, nb)
			for i := range st.Params.Bands {
				if st.Params.Bands[i], err = f32(); err != nil {
					return nil, err
				}
			}
			b, err := u8()
			if err != nil {
				return nil, err
			}
			st.Params.Loudness = b != 0
			if b, err = u8(); err != nil {
				return nil, err
			}
			st.Params.LimiterEnabled = b != 0
			if st.Params.LimiterThresholdDB, err = f32(); err != nil {
				return nil, err
			}
			if st.Params.LimiterReleaseMS, err = f32(); err != nil {
				return nil, err
			}
			if b, err = u8(); err != nil {
				return nil, err
			}
			st.Params.GuardEnabled = b != 0
			if st.Params.GuardDB, err = f32(); err != nil {
				return nil, err
			}
			if st.In, err = pcm(); err != nil {
				return nil, err
			}
			if st.Want, err = pcm(); err != nil {
				return nil, err
			}
			cs.Steps = append(cs.Steps, st)
		}
		if cs.Tail, err = pcm(); err != nil {
			return nil, err
		}
		c.Cases = append(c.Cases, cs)
	}

	if p != len(raw) {
		return nil, fmt.Errorf("fixture %s: %d bytes left over after %d cases",
			path, len(raw)-p, len(c.Cases))
	}
	return c, nil
}

// ─── Agreement ───────────────────────────────────────────────────────────────

// Tolerance for an implementation against the reference.
//
// EXPRESSED AS ERROR ENERGY BELOW THE SIGNAL, not as a per-sample integer
// difference, because that is the quantity that decides whether anyone can
// hear it. The reference runs float64 through scipy; a device implementation
// will run float32 or fixed point, so an exact match is not the goal and
// demanding one would fail on arithmetic that is inaudibly different. Error at
// −80dBFS relative to the signal sits below the noise floor of a 16-bit stream
// (−96dB) plus the room, and far below anything a small speaker resolves.
//
// PeakDiff is kept as a SECOND, independent criterion, and WHICH ONE BINDS
// DEPENDS ON LENGTH. A single bad sample of magnitude e contributes
// e/√N to the error RMS, so energy's sensitivity to it dilutes as the
// comparison gets longer: over one 2048-sample chunk the −80dB criterion
// already rejects a lone error above ~36 LSB, but over ten seconds of audio it
// tolerates one near 400 — which is an audible click that the energy figure
// calls clean. Peak is scale-free and length-free, so it is what catches a
// wrapped accumulator or a sign flip in a long run. Energy is the stricter
// criterion per chunk; peak is the one that still works per stream.
//
// BOTH NUMBERS ARE PROVISIONAL until a real port measures against them. They
// are set where they are because they are defensible, not because anything has
// been observed at them yet — the first implementation should report its
// actual figures and these should be tightened to just above them. A tolerance
// nobody has ever come close to is a tolerance that has stopped testing
// anything.
const (
	MaxErrorDB  = -80.0
	MaxPeakDiff = 64 // LSB, ~0.2% of full scale
)

// Result is how far an implementation sat from the reference.
type Result struct {
	// ErrorDB is 20·log₁₀(rms(got−want) / rms(want)). More negative is
	// better. −Inf means bit-identical.
	ErrorDB float64
	// PeakDiff is the largest single-sample absolute difference, in LSB.
	PeakDiff int
	// Samples compared.
	N int
}

// OK reports whether the result is within both tolerances.
func (r Result) OK() bool {
	return r.ErrorDB <= MaxErrorDB && r.PeakDiff <= MaxPeakDiff
}

func (r Result) String() string {
	return fmt.Sprintf("%d samples, error %.1f dB, peak diff %d LSB",
		r.N, r.ErrorDB, r.PeakDiff)
}

// Compare measures got against the reference want.
//
// Length disagreement is an error rather than a comparison over the shorter
// slice: the chain's latency is part of its behaviour, and an implementation
// that emits a different number of samples has a real bug that silently
// truncating the comparison would hide.
func Compare(got, want []int16) (Result, error) {
	if len(got) != len(want) {
		return Result{}, fmt.Errorf("length mismatch: got %d samples, want %d",
			len(got), len(want))
	}
	if len(want) == 0 {
		return Result{ErrorDB: math.Inf(-1)}, nil
	}
	var sumErr, sumRef float64
	peak := 0
	for i := range want {
		d := int(got[i]) - int(want[i])
		if d < 0 {
			d = -d
		}
		if d > peak {
			peak = d
		}
		fd := float64(int(got[i]) - int(want[i]))
		sumErr += fd * fd
		fr := float64(want[i])
		sumRef += fr * fr
	}
	n := float64(len(want))
	rmsErr := math.Sqrt(sumErr / n)
	rmsRef := math.Sqrt(sumRef / n)

	r := Result{PeakDiff: peak, N: len(want)}
	switch {
	case rmsErr == 0:
		r.ErrorDB = math.Inf(-1)
	case rmsRef == 0:
		// Reference is digital silence and the implementation is not. That is
		// a real failure, not a ratio — report it as such rather than
		// dividing by zero.
		r.ErrorDB = math.Inf(1)
	default:
		r.ErrorDB = 20 * math.Log10(rmsErr/rmsRef)
	}
	return r, nil
}
