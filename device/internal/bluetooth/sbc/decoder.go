package sbc

import (
	"errors"
	"fmt"
)

// Decoder decodes SBC frames to interleaved int16 PCM, following the spec's
// decoder flow independently of the encoder's arithmetic, so that
// each can check the other and both can be checked against libsbc.
type Decoder struct {
	v    [2][160]float32 // synthesis history per channel
	last Config
	have bool
}

// NewDecoder returns a Decoder with empty filter history.
func NewDecoder() *Decoder { return &Decoder{} }

// Decode decodes the frame at the start of src into pcm, which must hold
// SamplesPerFrame samples per channel. It returns the frame's configuration
// and the bytes consumed. A change of configuration resets the history.
func (d *Decoder) Decode(src []byte, pcm []int16) (Config, int, error) {
	c, err := ParseHeader(src)
	if err != nil {
		return c, 0, err
	}
	n := c.FrameLength()
	if len(src) < n {
		return c, 0, fmt.Errorf("sbc: %d bytes for a frame of %d", len(src), n)
	}
	ch, nsb := c.Channels(), c.Subbands
	if len(pcm) < c.SamplesPerFrame()*ch {
		return c, 0, errors.New("sbc: pcm buffer too small")
	}
	if src[3] != frameCRC(src, c.sideBits()) {
		return c, 0, ErrCRC
	}
	if !d.have || d.last != c {
		d.v = [2][160]float32{}
		d.last, d.have = c, true
	}

	r := bitReader{buf: src[:n], pos: headerLen}
	var join uint32
	if c.Mode == JointStereo {
		join = r.get(uint(nsb))
	}
	var sf, bits [2][8]int
	for i := 0; i < ch; i++ {
		for sb := 0; sb < nsb; sb++ {
			sf[i][sb] = int(r.get(4))
		}
	}
	allocate(&c, &sf, &bits)

	// sb_sample = scalefactor * ((2*audio_sample + 1) / levels - 1)
	var step, sfv [2][8]float32
	for i := 0; i < ch; i++ {
		for sb := 0; sb < nsb; sb++ {
			if bits[i][sb] == 0 {
				continue
			}
			levels := float32(int32(1)<<uint(bits[i][sb]) - 1)
			sfv[i][sb] = pow2[sf[i][sb]+1]
			step[i][sb] = sfv[i][sb] / levels
		}
	}
	var s [2][8]float32
	for blk := 0; blk < c.Blocks; blk++ {
		for i := 0; i < ch; i++ {
			for sb := 0; sb < nsb; sb++ {
				s[i][sb] = 0
				if nb := bits[i][sb]; nb > 0 {
					a := float32(r.get(uint(nb)))
					s[i][sb] = (2*a+1)*step[i][sb] - sfv[i][sb]
				}
			}
		}
		for sb := 0; sb < nsb; sb++ {
			if join&(1<<uint(nsb-1-sb)) != 0 {
				m, d := s[0][sb], s[1][sb]
				s[0][sb], s[1][sb] = m+d, m-d
			}
		}
		for i := 0; i < ch; i++ {
			out := pcm[blk*nsb*ch+i:]
			if nsb == 8 {
				d.synth8(i, &s[i], out, ch)
			} else {
				d.synth4(i, &s[i], out, ch)
			}
		}
	}
	return c, n, nil
}

// synth8 is the 8-subband synthesis filter. Its window is the
// analysis table scaled by -M, D[i] = -8*C[i]: the sign and gain at which
// libsbc's decode is reproduced (TestDecoderMatchesReferenceDecoder).
func (d *Decoder) synth8(ch int, s *[8]float32, out []int16, stride int) {
	v := &d.v[ch]
	copy(v[16:160], v[:144])
	for k := 0; k < 16; k++ {
		n := &synMatrix8[k]
		var acc float32
		for i := 0; i < 8; i++ {
			acc += n[i] * s[i]
		}
		v[k] = acc
	}
	var u [80]float32
	for i := 0; i < 5; i++ {
		for j := 0; j < 8; j++ {
			u[i*16+j] = v[i*32+j]
			u[i*16+j+8] = v[i*32+j+24]
		}
	}
	for j := 0; j < 8; j++ {
		var acc float32
		for i := 0; i < 10; i++ {
			acc += u[j+8*i] * proto8[j+8*i]
		}
		out[j*stride] = toPCM(acc * -8)
	}
}

func (d *Decoder) synth4(ch int, s *[8]float32, out []int16, stride int) {
	v := &d.v[ch]
	copy(v[8:80], v[:72])
	for k := 0; k < 8; k++ {
		n := &synMatrix4[k]
		var acc float32
		for i := 0; i < 4; i++ {
			acc += n[i] * s[i]
		}
		v[k] = acc
	}
	var u [40]float32
	for i := 0; i < 5; i++ {
		for j := 0; j < 4; j++ {
			u[i*8+j] = v[i*16+j]
			u[i*8+j+4] = v[i*16+j+12]
		}
	}
	for j := 0; j < 4; j++ {
		var acc float32
		for i := 0; i < 10; i++ {
			acc += u[j+4*i] * proto4[j+4*i]
		}
		out[j*stride] = toPCM(acc * -4)
	}
}

func toPCM(v float32) int16 {
	if v >= 0 {
		v += 0.5
	} else {
		v -= 0.5
	}
	if v > 32767 {
		return 32767
	}
	if v < -32768 {
		return -32768
	}
	return int16(v)
}

// bitReader reads MSB-first fields; reads past the end return zeros.
type bitReader struct {
	buf []byte
	pos int
	acc uint64
	n   uint
}

func (r *bitReader) get(bits uint) uint32 {
	for r.n < bits {
		var b byte
		if r.pos < len(r.buf) {
			b = r.buf[r.pos]
		}
		r.pos++
		r.acc = r.acc<<8 | uint64(b)
		r.n += 8
	}
	r.n -= bits
	return uint32(r.acc>>r.n) & (1<<bits - 1)
}
