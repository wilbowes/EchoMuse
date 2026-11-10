package sendspin

import (
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"

	"github.com/mewkiz/flac/frame"

	"github.com/wilbowes/EchoMuse/internal/opus"
)

// What the device asks for, best first (Wil, 2026-11-10: stereo from the
// source, downmixed on the device where the output is mono). Stereo Opus is a
// third of mono FLAC's bytes on the WiFi link and cost less CPU to decode on
// VVV (2.1% of one core against 3.3%); mono FLAC stays for a server that
// cannot encode Opus. One rate and bit depth, so the server resamples for us
// (MA does it per client anyway).
//
// The speaker is mono, so every stream is decoded to mono: Opus by libopus,
// FLAC and PCM by averaging the sides.
const (
	outRate     = 48000
	outChannels = 1 // what the decoder produces
	outBits     = 16
)

// offered is supportedFormats limited to codecs, in their order; all of them
// when codecs is empty.
func offered(codecs []string) []audioFormat {
	all := supportedFormats()
	if len(codecs) == 0 {
		return all
	}
	var out []audioFormat
	for _, c := range codecs {
		for _, f := range all {
			if f.Codec == c {
				out = append(out, f)
			}
		}
	}
	return out
}

func supportedFormats() []audioFormat {
	return []audioFormat{
		{Codec: "opus", Channels: 2, SampleRate: outRate, BitDepth: outBits},
		{Codec: "flac", Channels: 1, SampleRate: outRate, BitDepth: outBits},
	}
}

// streamFormat is what a stream/start said the chunks are.
type streamFormat struct {
	Codec      string `json:"codec"`
	SampleRate int    `json:"sample_rate"`
	Channels   int    `json:"channels"`
	BitDepth   int    `json:"bit_depth"`
	Header     string `json:"codec_header,omitempty"`
}

// decoder turns one chunk of the current format into mono S16 at outRate.
// Close it when its stream ends; reset it when the stream is cut.
type decoder struct {
	f    streamFormat
	op   *opus.Decoder // Opus streams only
	opus []int16       // scratch, one packet's worth
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
	case "opus":
		op, err := opus.NewDecoder(outChannels)
		if err != nil {
			return nil, err
		}
		return &decoder{f: f, op: op, opus: make([]int16, opus.MaxFrame*outChannels)}, nil
	default:
		return nil, fmt.Errorf("%w: codec %q", errFormat, f.Codec)
	}
	return &decoder{f: f}, nil
}

// reset forgets the audio before a cut (stream/clear: a seek or a skip), so
// Opus does not predict the next packet from audio that will not play.
// FLAC frames and PCM stand alone and need nothing.
func (d *decoder) reset() {
	if d != nil && d.op != nil {
		d.op.Reset()
	}
}

// close frees the Opus decoder's C state. Safe on nil and twice.
func (d *decoder) close() {
	if d != nil && d.op != nil {
		d.op.Close()
	}
}

// decode appends the chunk's samples to dst as mono. A stereo stream (which
// the device never asks for, but a server may send) is averaged.
func (d *decoder) decode(dst []int16, payload []byte) ([]int16, error) {
	if d.op != nil {
		// One packet per chunk, as aiosendspin sends them. The server has
		// already moved the stream's timestamps by the encoder's pre-skip.
		n, err := d.op.Decode(payload, d.opus)
		if err != nil {
			return dst, err
		}
		return append(dst, d.opus[:n*outChannels]...), nil
	}
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
// bytes. The spec, and aiosendspin from 10.0 (Music Assistant 2.10 ships
// 10.2.1), add a uint32 send_ahead, making it thirteen. Nothing in the
// handshake says which, so the payload tells:
//
//   - FLAC: by the frame sync code (0xFFF8 fixed-blocksize, 0xFFF9
//     variable) that every chunk starts with: whichever offset it sits at is
//     where the payload begins.
//   - Opus: by byte 9. In thirteen it is send_ahead's top byte, zero for any
//     lead under 16.7s. In nine it is the packet's TOC, and a TOC of zero is
//     SILK narrowband 10ms mono, which no encoder produces for 48kHz stereo
//     music at the bitrates a server picks.
//
// A server that upgrades then keeps working.
func chunkPayload(msg []byte, codec string) (ts int64, payload []byte, err error) {
	if len(msg) < 9 {
		return 0, nil, errors.New("sendspin: audio chunk shorter than its header")
	}
	ts = int64(binary.BigEndian.Uint64(msg[1:9]))
	if len(msg) >= 13 {
		switch codec {
		case "flac":
			if !flacSync(msg[9:]) && flacSync(msg[13:]) {
				return ts, msg[13:], nil
			}
		case "opus":
			if msg[9] == 0 {
				if len(msg) == 13 {
					return 0, nil, errors.New("sendspin: audio chunk with no payload")
				}
				return ts, msg[13:], nil
			}
		}
	}
	return ts, msg[9:], nil
}

func flacSync(b []byte) bool { return len(b) >= 2 && b[0] == 0xFF && b[1]&0xFE == 0xF8 }
