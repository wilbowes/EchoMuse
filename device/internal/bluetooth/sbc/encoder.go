package sbc

import "fmt"

// Encoder turns interleaved int16 PCM into SBC frames. Every buffer is fixed
// size and owned by the Encoder, so Encode allocates nothing.
type Encoder struct {
	cfg      Config
	ch, nsb  int
	frameLen int

	// Analysis state per channel: the newest 10*nsb input samples, X[0]
	// newest.
	x [2][80]float32

	sb   [16][2][8]float32 // subband samples [block][channel][subband]
	sf   [2][8]int
	bits [2][8]int
	join uint8 // bit (nsb-1-sb) set when subband sb is joint coded
}

// NewEncoder validates c and returns an Encoder for it.
func NewEncoder(c Config) (*Encoder, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	return &Encoder{cfg: c, ch: c.Channels(), nsb: c.Subbands, frameLen: c.FrameLength()}, nil
}

// Config is the configuration every frame is encoded with.
func (e *Encoder) Config() Config { return e.cfg }

// Reset clears the filter history, as at the start of a new stream.
func (e *Encoder) Reset() { e.x = [2][80]float32{} }

// Encode encodes exactly one frame: pcm holds SamplesPerFrame samples per
// channel, interleaved. It writes FrameLength bytes to dst and returns that
// count.
func (e *Encoder) Encode(dst []byte, pcm []int16) (int, error) {
	if want := e.cfg.SamplesPerFrame() * e.ch; len(pcm) != want {
		return 0, fmt.Errorf("sbc: %d samples for a frame of %d", len(pcm), want)
	}
	if len(dst) < e.frameLen {
		return 0, fmt.Errorf("sbc: %d bytes for a frame of %d", len(dst), e.frameLen)
	}
	blocks := e.cfg.Blocks
	for blk := 0; blk < blocks; blk++ {
		base := blk * e.nsb * e.ch
		for ch := 0; ch < e.ch; ch++ {
			if e.nsb == 8 {
				e.analyse8(ch, pcm[base+ch:], &e.sb[blk][ch])
			} else {
				e.analyse4(ch, pcm[base+ch:], &e.sb[blk][ch])
			}
		}
	}
	e.join = 0
	if e.cfg.Mode == JointStereo {
		e.chooseJoin()
	}
	e.scaleFactors()
	allocate(&e.cfg, &e.sf, &e.bits)
	e.pack(dst[:e.frameLen])
	return e.frameLen, nil
}

// analyse8 runs one block of the 8-subband analysis filter for one channel.
// pcm[0] is the block's first sample of this channel; samples are e.ch apart.
func (e *Encoder) analyse8(ch int, pcm []int16, out *[8]float32) {
	x := &e.x[ch]
	copy(x[8:], x[:72])
	for i, j := 7, 0; i >= 0; i, j = i-1, j+e.ch {
		x[i] = float32(pcm[j])
	}
	var y [16]float32
	for i := 0; i < 16; i++ {
		y[i] = proto8[i]*x[i] + proto8[i+16]*x[i+16] + proto8[i+32]*x[i+32] +
			proto8[i+48]*x[i+48] + proto8[i+64]*x[i+64]
	}
	for i := 0; i < 8; i++ {
		m := &anaMatrix8[i]
		var s float32
		for k := 0; k < 16; k++ {
			s += m[k] * y[k]
		}
		out[i] = s
	}
}

func (e *Encoder) analyse4(ch int, pcm []int16, out *[8]float32) {
	x := &e.x[ch]
	copy(x[4:40], x[:36])
	for i, j := 3, 0; i >= 0; i, j = i-1, j+e.ch {
		x[i] = float32(pcm[j])
	}
	var y [8]float32
	for i := 0; i < 8; i++ {
		y[i] = proto4[i]*x[i] + proto4[i+8]*x[i+8] + proto4[i+16]*x[i+16] +
			proto4[i+24]*x[i+24] + proto4[i+32]*x[i+32]
	}
	for i := 0; i < 4; i++ {
		m := &anaMatrix4[i]
		var s float32
		for k := 0; k < 8; k++ {
			s += m[k] * y[k]
		}
		out[i] = s
	}
}

// scaleFactor is the smallest sf with |sample| < 2^(sf+1), capped at 15
// (scalefactor = 2^(scale_factor+1)).
func scaleFactor(peak float32) int {
	sf := 0
	for sf < 15 && peak >= pow2[sf+1] {
		sf++
	}
	return sf
}

func abs32(v float32) float32 {
	if v < 0 {
		return -v
	}
	return v
}

// chooseJoin codes a subband as mid/side when that needs smaller scale
// factors, then transforms it in place. The last subband is never joined:
// its join bit is reserved.
func (e *Encoder) chooseJoin() {
	for sb := 0; sb < e.nsb-1; sb++ {
		var pl, pr, pm, ps float32
		for blk := 0; blk < e.cfg.Blocks; blk++ {
			l, r := e.sb[blk][0][sb], e.sb[blk][1][sb]
			pl = max32(pl, abs32(l))
			pr = max32(pr, abs32(r))
			pm = max32(pm, abs32((l+r)*0.5))
			ps = max32(ps, abs32((l-r)*0.5))
		}
		if scaleFactor(pm)+scaleFactor(ps) >= scaleFactor(pl)+scaleFactor(pr) {
			continue
		}
		e.join |= 1 << uint(e.nsb-1-sb)
		for blk := 0; blk < e.cfg.Blocks; blk++ {
			l, r := e.sb[blk][0][sb], e.sb[blk][1][sb]
			e.sb[blk][0][sb] = (l + r) * 0.5
			e.sb[blk][1][sb] = (l - r) * 0.5
		}
	}
}

func max32(a, b float32) float32 {
	if b > a {
		return b
	}
	return a
}

func (e *Encoder) scaleFactors() {
	for ch := 0; ch < e.ch; ch++ {
		for sb := 0; sb < e.nsb; sb++ {
			var peak float32
			for blk := 0; blk < e.cfg.Blocks; blk++ {
				peak = max32(peak, abs32(e.sb[blk][ch][sb]))
			}
			e.sf[ch][sb] = scaleFactor(peak)
		}
	}
}

// pack writes header, side information, CRC and samples (header, side information, samples),
// zero-padded to the frame length.
func (e *Encoder) pack(dst []byte) {
	b1, b2 := e.cfg.header()
	dst[0], dst[1], dst[2], dst[3] = syncword, b1, b2, 0
	w := bitWriter{buf: dst, pos: headerLen}
	if e.cfg.Mode == JointStereo {
		w.put(uint32(e.join), uint(e.nsb))
	}
	for ch := 0; ch < e.ch; ch++ {
		for sb := 0; sb < e.nsb; sb++ {
			w.put(uint32(e.sf[ch][sb]), 4)
		}
	}
	var invSF [2][8]float32
	var half [2][8]float32
	var top [2][8]int32
	for ch := 0; ch < e.ch; ch++ {
		for sb := 0; sb < e.nsb; sb++ {
			invSF[ch][sb] = invPow2[e.sf[ch][sb]+1]
			lv := int32(1)<<uint(e.bits[ch][sb]) - 1
			top[ch][sb] = lv - 1
			half[ch][sb] = float32(lv) * 0.5
		}
	}
	for blk := 0; blk < e.cfg.Blocks; blk++ {
		for ch := 0; ch < e.ch; ch++ {
			for sb := 0; sb < e.nsb; sb++ {
				nb := e.bits[ch][sb]
				if nb == 0 {
					continue
				}
				// floor((s/SF + 1) * levels/2): the bin whose midpoint the
				// decoder reconstructs, (2q+1)/levels - 1.
				q := int32((e.sb[blk][ch][sb]*invSF[ch][sb] + 1) * half[ch][sb])
				if q > top[ch][sb] {
					q = top[ch][sb]
				} else if q < 0 {
					q = 0
				}
				w.put(uint32(q), uint(nb))
			}
		}
	}
	w.flush()
	for i := w.pos; i < len(dst); i++ {
		dst[i] = 0
	}
	// The CRC reads only the side bits, so it can follow the samples.
	dst[3] = frameCRC(dst, e.cfg.sideBits())
}

// bitWriter packs MSB-first fields into buf from byte pos.
type bitWriter struct {
	buf []byte
	pos int
	acc uint64
	n   uint
}

func (w *bitWriter) put(v uint32, bits uint) {
	w.acc = w.acc<<bits | uint64(v)
	w.n += bits
	for w.n >= 8 {
		w.n -= 8
		w.buf[w.pos] = byte(w.acc >> w.n)
		w.pos++
	}
}

func (w *bitWriter) flush() {
	if w.n > 0 {
		w.buf[w.pos] = byte(w.acc << (8 - w.n))
		w.pos++
		w.n = 0
	}
}
