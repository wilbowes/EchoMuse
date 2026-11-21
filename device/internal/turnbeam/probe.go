package turnbeam

import (
	"math"
	"math/cmplx"
)

// A Probe is the second look at a wake near-miss: the turn's own beam (the
// same steering and MVDR Lock would compute) aimed at the near-miss's wake
// window and run over the recent input, for the wake word scorer to score
// once more. One beam, from one candidate, never a choice among several.
//
// Offline on 41C8 in the lounge with the TV on (37 attempts, 2026-11-21):
// raw mics woke 18; with a second look at near-misses scoring 0.1-0.5, 21,
// with no TV false wakes in ~6 min of TV and the quiet room unchanged (10/10).
//
// Take one on the capture goroutine (it reads the stage); Beam is safe to
// run anywhere after that.
type Probe struct {
	b     *beamSet
	m     int
	in    [][]float64 // per mic, one hop of history then the clip
	start float64     // time around the crossing of the first sample Beam returns
}

// ProbeS is how much audio before the crossing a probe beams: enough for the
// scorer to settle before the wake word.
const ProbeS = 3.0

// Probe takes a second look at a crossing leadS ago (at most MaxLeadS).
func (s *Stage) Probe(leadS float64) (*Probe, error) {
	leadS = math.Max(0, leadS)
	if leadS > MaxLeadS {
		return nil, errTooLate
	}
	clipN := (int((leadS+ProbeS)*sampleRate) + hopH - 1) / hopH * hopH
	if s.inCount < clipN+hopH {
		return nil, errTooEarly
	}
	b, err := s.beamFor(leadS)
	if err != nil {
		return nil, err
	}
	n := len(s.in[0])
	from := (s.inHead - clipN - hopH + 2*n) % n
	p := &Probe{b: b, m: s.m, in: make([][]float64, s.m)}
	for c := 0; c < s.m; c++ {
		p.in[c] = make([]float64, clipN+hopH)
		for i := range p.in[c] {
			p.in[c][i] = float64(s.in[c][(from+i)%n])
		}
	}
	// Beam's first sample is p.in[0][0], clipN+hopH before now; the crossing
	// is leadS before now.
	p.start = leadS - float64(clipN+hopH)/sampleRate
	return p, nil
}

// Start is the time, around the crossing, of Beam's first sample.
func (p *Probe) Start() float64 { return p.start }

// Beam returns the beamformed clip: MVDR only (the wake word scorer was
// trained on plain audio, not on post-filtered audio), with the talker at
// the mics' average gain. Distortionless on one reference mic instead, the
// verdict depended on which mic that was: a faint wake word scored 0.02 to
// 0.84 across the four (41C8 offline, 2026-11-21).
func (p *Probe) Beam() []int16 {
	m := p.m
	f := newFFT(frameN)
	win := make([]float64, frameN)
	for i := range win {
		win[i] = math.Sqrt(0.5 - 0.5*math.Cos(2*math.Pi*float64(i)/frameN))
	}
	re := make([][]float64, m)
	im := make([][]float64, m)
	for c := range re {
		re[c], im[c] = make([]float64, frameN), make([]float64, frameN)
	}
	ola := make([]float64, frameN)
	ore, oim := make([]float64, frameN), make([]float64, frameN)
	n := len(p.in[0]) - hopH
	out := make([]int16, 0, n)
	for off := hopH; off+hopH <= len(p.in[0]); off += hopH {
		for c := 0; c < m; c++ {
			copy(re[c], p.in[c][off-hopH:off+hopH])
			for i := range re[c] {
				re[c][i] *= win[i]
				im[c][i] = 0
			}
			f.forward(re[c], im[c])
		}
		for k := 0; k < bins; k++ {
			var y complex128
			for c := 0; c < m; c++ {
				y += cmplx.Conj(p.b.h[k][c]) * complex(re[c][k], im[c][k])
			}
			if g := p.b.norm[k]; g > 0 {
				y *= complex(g, 0)
			}
			if k == 0 || k == bins-1 {
				y = complex(real(y), 0)
			}
			ore[k], oim[k] = real(y), imag(y)
			if k > 0 && k < bins-1 {
				ore[frameN-k], oim[frameN-k] = real(y), -imag(y)
			}
		}
		f.inverse(ore, oim)
		for i := 0; i < frameN; i++ {
			ola[i] += ore[i] * win[i]
		}
		for i := 0; i < hopH; i++ {
			out = append(out, clip16(ola[i]))
		}
		copy(ola, ola[hopH:])
		for i := hopH; i < frameN; i++ {
			ola[i] = 0
		}
	}
	return out
}
