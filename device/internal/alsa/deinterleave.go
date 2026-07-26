package alsa

// ExtractS24_3LE copies one channel out of an interleaved S24_3LE buffer into
// out, sign-extending each 24-bit sample to int32. It returns the number of
// frames written.
//
// Callers pass a reusable out slice; nothing is allocated per period.
func ExtractS24_3LE(buf []byte, channels, ch int, out []int32) int {
	frameBytes := channels * 3
	frames := len(buf) / frameBytes
	if frames > len(out) {
		frames = len(out)
	}
	for f := 0; f < frames; f++ {
		o := f*frameBytes + ch*3
		v := int32(buf[o]) | int32(buf[o+1])<<8 | int32(buf[o+2])<<16
		if v&0x800000 != 0 {
			v -= 1 << 24
		}
		out[f] = v
	}
	return frames
}
