// Package opus decodes Opus packets (RFC 6716) with libopus 1.5.2, vendored
// decoder-only under libopus/ (vendor.sh re-creates it from the release,
// sha256-checked). Fixed point, with NEON on ARM: every Echo is a Cortex-A53
// or A35. Decoding 128 kbps stereo cost 2.1% of one core on VVV, against 3.3%
// for the mono FLAC it replaces (docs/history/device.md, 2026-11-10).
package opus

/*
#cgo CFLAGS: -I${SRCDIR}/libopus/include -I${SRCDIR}/libopus/celt -I${SRCDIR}/libopus/silk -I${SRCDIR}/libopus/silk/fixed -I${SRCDIR}/libopus
#cgo CFLAGS: -O2 -DOPUS_BUILD -DFIXED_POINT -DUSE_ALLOCA -DHAVE_LRINTF -DHAVE_LRINT -DDISABLE_FLOAT_API -w
#cgo arm CFLAGS: -mfpu=neon -DOPUS_ARM_MAY_HAVE_NEON_INTR -DOPUS_ARM_PRESUME_NEON_INTR
#cgo LDFLAGS: -lm
#include <stdlib.h>
#include "opus.h"
// opus_decoder_ctl is variadic, which cgo cannot call.
static int opus_decoder_ctl_reset(OpusDecoder *d) { return opus_decoder_ctl(d, OPUS_RESET_STATE); }
*/
import "C"

import (
	"errors"
	"fmt"
	"unsafe"
)

// Rate is the only rate this package decodes at; Opus decodes any stream at
// any of its rates, and 48 kHz is the speaker's.
const Rate = 48000

// MaxFrame is the most samples per channel one packet can hold: 120 ms.
const MaxFrame = Rate * 120 / 1000

// Decoder holds one stream's state. Not safe for concurrent use. Reset it
// whenever the stream it decodes is cut (a seek, a clear, a new stream):
// otherwise the first frames after the cut are predicted from audio that is
// no longer there.
type Decoder struct {
	d        *C.OpusDecoder
	channels int
}

// NewDecoder makes a decoder producing channels (1 or 2) interleaved S16
// samples at Rate. A stereo packet decoded to one channel is downmixed by
// libopus, and a mono packet to two is duplicated.
func NewDecoder(channels int) (*Decoder, error) {
	if channels != 1 && channels != 2 {
		return nil, fmt.Errorf("opus: %d channels, want 1 or 2", channels)
	}
	var e C.int
	d := C.opus_decoder_create(Rate, C.int(channels), &e)
	if e != C.OPUS_OK || d == nil {
		return nil, codeErr(e)
	}
	return &Decoder{d: d, channels: channels}, nil
}

// Channels is how many channels Decode writes per frame.
func (d *Decoder) Channels() int { return d.channels }

// Decode decodes one packet into pcm (interleaved, len a multiple of the
// channel count) and returns the samples written per channel. An empty packet
// asks for loss concealment of one 20 ms frame. pcm must hold the packet's
// whole duration, MaxFrame per channel at most, or the packet is refused.
func (d *Decoder) Decode(packet []byte, pcm []int16) (int, error) {
	if d.d == nil {
		return 0, errors.New("opus: decoder closed")
	}
	frame := len(pcm) / d.channels
	if frame == 0 {
		return 0, errors.New("opus: no room to decode into")
	}
	var data *C.uchar
	if len(packet) > 0 {
		data = (*C.uchar)(unsafe.Pointer(&packet[0]))
	} else if frame > Rate/50 {
		frame = Rate / 50 // conceal one 20 ms frame, not the whole buffer
	}
	n := C.opus_decode(d.d, data, C.opus_int32(len(packet)),
		(*C.opus_int16)(unsafe.Pointer(&pcm[0])), C.int(frame), 0)
	if n < 0 {
		return 0, codeErr(n)
	}
	return int(n), nil
}

// Reset returns the decoder to its just-created state.
func (d *Decoder) Reset() {
	if d.d != nil {
		C.opus_decoder_ctl_reset(d.d)
	}
}

// Close frees the decoder. Safe to call twice.
func (d *Decoder) Close() {
	if d.d != nil {
		C.opus_decoder_destroy(d.d)
		d.d = nil
	}
}

// PacketSamples is how many samples per channel packet decodes to at Rate,
// read from its TOC without decoding it.
func PacketSamples(packet []byte) (int, error) {
	if len(packet) == 0 {
		return 0, errors.New("opus: empty packet")
	}
	n := C.opus_packet_get_nb_samples((*C.uchar)(unsafe.Pointer(&packet[0])),
		C.opus_int32(len(packet)), Rate)
	if n < 0 {
		return 0, codeErr(n)
	}
	return int(n), nil
}

func codeErr(c C.int) error {
	return fmt.Errorf("opus: %s", C.GoString(C.opus_strerror(c)))
}
