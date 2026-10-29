package sbc

// Bit allocation: how a frame's bitpool is shared across subbands,
// derived from the scale factors alone, so encoder and decoder agree without
// sending it. The loops follow the spec's pseudo-code step for step; a
// "simplification" that spends one bit differently desynchronises every
// decoder.

// Loudness offsets per sampling frequency and subband (the offset4 and
// offset8 tables).
var offset4 = [4][4]int{
	{-1, 0, 0, 0},
	{-2, 0, 0, 1},
	{-2, 0, 0, 1},
	{-2, 0, 0, 1},
}

var offset8 = [4][8]int{
	{-2, 0, 0, 0, 0, 0, 0, 1},
	{-3, 0, 0, 0, 0, 0, 1, 2},
	{-4, 0, 0, 0, 0, 0, 1, 2},
	{-4, 0, 0, 0, 0, 0, 1, 2},
}

// bitNeed fills need for one channel and returns its maximum (never below 0,
// as in the spec).
func bitNeed(alloc Allocation, fs, nsb int, sf, need *[8]int) int {
	max := 0
	for sb := 0; sb < nsb; sb++ {
		n := sf[sb]
		if alloc == Loudness {
			if n == 0 {
				n = -5
			} else {
				var off int
				if nsb == 4 {
					off = offset4[fs][sb]
				} else {
					off = offset8[fs][sb]
				}
				n -= off
				if n > 0 {
					n >>= 1 // positive, so the shift is the spec's /2
				}
			}
		}
		need[sb] = n
		if n > max {
			max = n
		}
	}
	return max
}

// slices counts the bits one bitslice step would add for a channel.
func slices(need *[8]int, nsb, bitslice int) int {
	count := 0
	for sb := 0; sb < nsb; sb++ {
		n := need[sb]
		if n > bitslice+1 && n < bitslice+16 {
			count++
		} else if n == bitslice+1 {
			count += 2
		}
	}
	return count
}

// allocate fills bits from the scale factors. Mono and dual share a bitpool
// per channel; stereo and joint share one across both.
func allocate(c *Config, sf, bits *[2][8]int) {
	fs := freqIndex(c.SampleRate)
	nsb, pool := c.Subbands, c.Bitpool
	var need [2][8]int
	if c.Mode == Mono || c.Mode == DualChannel {
		for ch := 0; ch < c.Channels(); ch++ {
			max := bitNeed(c.Allocation, fs, nsb, &sf[ch], &need[ch])
			allocMono(&need[ch], &bits[ch], nsb, pool, max)
		}
		return
	}
	m0 := bitNeed(c.Allocation, fs, nsb, &sf[0], &need[0])
	m1 := bitNeed(c.Allocation, fs, nsb, &sf[1], &need[1])
	if m1 > m0 {
		m0 = m1
	}
	allocStereo(&need, bits, nsb, pool, m0)
}

func allocMono(need, bits *[8]int, nsb, pool, maxNeed int) {
	bitcount, slicecount := 0, 0
	bitslice := maxNeed + 1
	for {
		bitslice--
		bitcount += slicecount
		slicecount = slices(need, nsb, bitslice)
		if bitcount+slicecount >= pool {
			break
		}
	}
	if bitcount+slicecount == pool {
		bitcount += slicecount
		bitslice--
	}
	for sb := 0; sb < nsb; sb++ {
		bits[sb] = firstBits(need[sb], bitslice)
	}
	for sb := 0; bitcount < pool && sb < nsb; sb++ {
		if bits[sb] >= 2 && bits[sb] < 16 {
			bits[sb]++
			bitcount++
		} else if need[sb] == bitslice+1 && pool > bitcount+1 {
			bits[sb] = 2
			bitcount += 2
		}
	}
	for sb := 0; bitcount < pool && sb < nsb; sb++ {
		if bits[sb] < 16 {
			bits[sb]++
			bitcount++
		}
	}
}

func allocStereo(need, bits *[2][8]int, nsb, pool, maxNeed int) {
	bitcount, slicecount := 0, 0
	bitslice := maxNeed + 1
	for {
		bitslice--
		bitcount += slicecount
		slicecount = slices(&need[0], nsb, bitslice) + slices(&need[1], nsb, bitslice)
		if bitcount+slicecount >= pool {
			break
		}
	}
	if bitcount+slicecount == pool {
		bitcount += slicecount
		bitslice--
	}
	for ch := 0; ch < 2; ch++ {
		for sb := 0; sb < nsb; sb++ {
			bits[ch][sb] = firstBits(need[ch][sb], bitslice)
		}
	}
	// The spec walks left then right within each subband.
	ch, sb := 0, 0
	for bitcount < pool && sb < nsb {
		if bits[ch][sb] >= 2 && bits[ch][sb] < 16 {
			bits[ch][sb]++
			bitcount++
		} else if need[ch][sb] == bitslice+1 && pool > bitcount+1 {
			bits[ch][sb] = 2
			bitcount += 2
		}
		if ch == 1 {
			ch, sb = 0, sb+1
		} else {
			ch = 1
		}
	}
	ch, sb = 0, 0
	for bitcount < pool && sb < nsb {
		if bits[ch][sb] < 16 {
			bits[ch][sb]++
			bitcount++
		}
		if ch == 1 {
			ch, sb = 0, sb+1
		} else {
			ch = 1
		}
	}
}

func firstBits(need, bitslice int) int {
	if need < bitslice+2 {
		return 0
	}
	if b := need - bitslice; b < 16 {
		return b
	}
	return 16
}
