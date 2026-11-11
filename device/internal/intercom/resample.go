// The peer link carries 48 kHz mono S16LE — the speaker's native rate —
// but the microphone pipeline produces 16 kHz mono S16LE. This file is
// the streaming upsampler between them.
//
// Linear interpolation, 3x: for each input sample x[n] it emits x[n]
// followed by two samples interpolated toward x[n+1]. It is deliberately
// the cheapest resampler that is not wrong: a Dot 2's ARM core cannot
// spare a polyphase filter for a whole call, and speech through a linear
// interpolator is intelligible — the alternative the design considered
// was a zero-order hold, which sounds noticeably worse for the same
// cost. A better filter (or FLAC/Opus, Phase 4) replaces this file, not
// its callers: the interface is Write([]byte) []byte, mono S16LE in,
// mono S16LE out, 3x the samples.
//
// The interpolator is stateful across frames: the two samples between
// the last sample of one frame and the first of the next need both
// samples, so the last sample of each frame is held as pending and
// emitted when the next frame arrives, one sample (62.5 µs) behind.
// Frames are the mic pipeline's 160 ms periods; a frame is never split
// across calls.
package intercom

import (
	"encoding/binary"
)

// Resampler upsamples 16 kHz mono S16LE to 48 kHz mono S16LE.
type Resampler struct {
	pending    int16
	hasPending bool
}

// Write upsamples one frame of 16 kHz mono S16LE and returns the 48 kHz
// frame. An odd trailing byte is dropped — mic frames are always whole
// samples, so it can only come from a corrupt source. The returned slice
// is freshly allocated; the input is not retained.
//
// The last sample of the frame is held, not emitted: its two
// interpolants need the next frame's first sample. So Write returns 3x
// the samples of the frame MINUS the held one, plus the previous frame's
// held one when there is one. Over a whole stream the output is exactly
// 3x the input; Flush emits the held sample (as three equal samples) at
// end of stream.
func (r *Resampler) Write(p []byte) []byte {
	n := len(p) / 2
	if n == 0 {
		return nil
	}
	// Samples emitted this call: the held pending one (3 samples) plus
	// every sample of this frame except its last (3 each).
	outSamples := (n - 1) * 3
	if r.hasPending {
		outSamples += 3
	}
	out := make([]byte, outSamples*2)
	o := 0
	emit := func(cur, next int16) {
		binary.LittleEndian.PutUint16(out[o:], uint16(cur))
		binary.LittleEndian.PutUint16(out[o+2:], uint16((2*int32(cur)+int32(next))/3))
		binary.LittleEndian.PutUint16(out[o+4:], uint16((int32(cur)+2*int32(next))/3))
		o += 6
	}
	if r.hasPending {
		emit(r.pending, int16(binary.LittleEndian.Uint16(p[0:2])))
	}
	for i := 0; i < n-1; i++ {
		cur := int16(binary.LittleEndian.Uint16(p[i*2:]))
		next := int16(binary.LittleEndian.Uint16(p[(i+1)*2:]))
		emit(cur, next)
	}
	r.pending = int16(binary.LittleEndian.Uint16(p[(n-1)*2:]))
	r.hasPending = true
	return out
}

// Flush emits the held last sample as three equal samples (a hold, not
// an interpolation — there is no next sample) and clears the resampler.
func (r *Resampler) Flush() []byte {
	if !r.hasPending {
		return nil
	}
	out := make([]byte, 6)
	for i := 0; i < 3; i++ {
		binary.LittleEndian.PutUint16(out[i*2:], uint16(r.pending))
	}
	r.pending = 0
	r.hasPending = false
	return out
}

// Reset drops the held sample. Call it when the mic stream restarts, so
// a stale sample cannot smear into the new stream.
func (r *Resampler) Reset() {
	r.pending = 0
	r.hasPending = false
}
