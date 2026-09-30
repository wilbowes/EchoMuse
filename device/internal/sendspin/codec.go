package sendspin

import (
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"

	"github.com/mewkiz/flac/frame"
)

// The one format the device asks for. A single rate and channel count, so the
// server resamples and downmixes for us: MA does it per client anyway, and a
// player that lists one format never has to switch output mid-stream. Mono
// because the speaker is mono and the wire then carries half the bytes.
//
// FLAC only. The spec requires a player to list flac or pcm; FLAC is ~40% of
// PCM's bitrate for 3.28% of a core (PR #271), on links measured at 4.6-7.1%
// loss. The audio-chunk header check in chunkPayload also leans on FLAC's
// sync code, which PCM does not have.
const (
	outRate     = 48000
	outChannels = 1
	outBits     = 16
)

// streamFormat is what a stream/start said the chunks are.
type streamFormat struct {
	Codec      string `json:"codec"`
	SampleRate int    `json:"sample_rate"`
	Channels   int    `json:"channels"`
	BitDepth   int    `json:"bit_depth"`
	Header     string `json:"codec_header,omitempty"`
}

// decoder turns one chunk of the current format into mono S16 at outRate.
type decoder struct {
	f streamFormat
}

var errFormat = errors.New("sendspin: stream format the device did not ask for")

func newDecoder(f streamFormat) (*decoder, error) {
	if f.SampleRate != outRate || f.BitDepth != outBits || f.Channels < 1 || f.Channels > 2 {
		return nil, fmt.Errorf("%w: %s %dHz %d-bit %dch", errFormat, f.Codec, f.SampleRate, f.BitDepth, f.Channels)
	}
	switch f.Codec {
	case "flac":
		if f.Header != "" {
			h, err := base64.StdEncoding.DecodeString(f.Header)
			if err != nil || len(h) < 4 || string(h[:4]) != "fLaC" {
				return nil, errors.New("sendspin: flac codec_header is not a FLAC stream header")
			}
		}
	case "pcm":
	default:
		return nil, fmt.Errorf("%w: codec %q", errFormat, f.Codec)
	}
	return &decoder{f: f}, nil
}

// decode appends the chunk's samples to dst as mono. A stereo stream (which
// the device never asks for, but a server may send) is averaged.
func (d *decoder) decode(dst []int16, payload []byte) ([]int16, error) {
	if d.f.Codec == "pcm" {
		n := len(payload) / 2 / d.f.Channels
		for i := 0; i < n; i++ {
			if d.f.Channels == 1 {
				dst = append(dst, int16(binary.LittleEndian.Uint16(payload[i*2:])))
				continue
			}
			l := int32(int16(binary.LittleEndian.Uint16(payload[i*4:])))
			r := int32(int16(binary.LittleEndian.Uint16(payload[i*4+2:])))
			dst = append(dst, int16((l+r)/2))
		}
		return dst, nil
	}
	// One or more whole FLAC frames. The frames carry their own sample rate
	// and bit depth codes, so the stream header is not needed to decode them.
	r := bytes.NewReader(payload)
	for r.Len() > 0 {
		fr, err := frame.Parse(r)
		if err == io.EOF {
			break
		}
		if err != nil {
			return dst, err
		}
		if len(fr.Subframes) == 0 {
			continue
		}
		n := len(fr.Subframes[0].Samples)
		if len(fr.Subframes) == 1 {
			for _, s := range fr.Subframes[0].Samples[:n] {
				dst = append(dst, int16(s))
			}
			continue
		}
		l, rr := fr.Subframes[0].Samples, fr.Subframes[1].Samples
		for i := 0; i < n; i++ {
			dst = append(dst, int16((l[i]+rr[i])/2))
		}
	}
	return dst, nil
}

// chunkPayload splits an audio chunk (type byte already checked) into its
// server timestamp and codec payload.
//
// 9.1.1: the header is the type byte and a big-endian int64 timestamp — nine
// bytes. The current spec adds a uint32 send_ahead, making it thirteen. The
// two are told apart by the FLAC frame sync code (0xFFF8 fixed-blocksize,
// 0xFFF9 variable), which every chunk starts with: whichever offset it sits
// at is where the payload begins. A server that upgrades then keeps working.
func chunkPayload(msg []byte, codec string) (ts int64, payload []byte, err error) {
	if len(msg) < 9 {
		return 0, nil, errors.New("sendspin: audio chunk shorter than its header")
	}
	ts = int64(binary.BigEndian.Uint64(msg[1:9]))
	if codec == "flac" && !flacSync(msg[9:]) && len(msg) >= 13 && flacSync(msg[13:]) {
		return ts, msg[13:], nil
	}
	return ts, msg[9:], nil
}

func flacSync(b []byte) bool { return len(b) >= 2 && b[0] == 0xFF && b[1]&0xFE == 0xF8 }
