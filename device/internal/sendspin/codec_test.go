package sendspin

import (
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"os"
	"slices"
	"testing"
)

// Chunks from aiosendspin's own FlacEncoder (testdata/gen_flac.py), which is
// what Music Assistant streams with. FLAC is lossless, so the decode must be
// bit-exact against the PCM that went in.
func TestFlacChunksFromTheServerEncoderDecodeBitExact(t *testing.T) {
	t.Run("mono", func(t *testing.T) { flacBitExact(t, "testdata/flac_chunks.json", 1) })
	// #273: two different channels, so the encoder uses its inter-channel
	// modes (gen_flac_stereo.py) and the decode must undo each of them.
	t.Run("stereo", func(t *testing.T) { flacBitExact(t, "testdata/flac_chunks_stereo.json", 2) })
}

// flacBitExact decodes a fixture and holds it to the PCM that went in. The
// decoder always yields stereo frames, so a mono fixture is expected on both
// channels.
func flacBitExact(t *testing.T, path string, channels int) {
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fx struct {
		Header string
		Chunks []struct {
			TS   int64
			Data string
		}
		PCM string
	}
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	d, err := newDecoder(streamFormat{Codec: "flac", SampleRate: 48000, Channels: channels, BitDepth: 16, Header: fx.Header})
	if err != nil {
		t.Fatal(err)
	}
	var got []int16
	for i, c := range fx.Chunks {
		b, _ := base64.StdEncoding.DecodeString(c.Data)
		if !flacSync(b) {
			t.Fatalf("chunk %d does not start with a FLAC frame sync", i)
		}
		if got, err = d.decode(got, b); err != nil {
			t.Fatalf("chunk %d: %v", i, err)
		}
	}
	pcm, _ := base64.StdEncoding.DecodeString(fx.PCM)
	want := len(pcm) / 2 / channels // frames
	// The encoder pads its last frame out to the block size on flush.
	if len(got)%2 != 0 || len(got)/2 < want || len(got)/2 > want+4608 {
		t.Fatalf("decoded %d samples (%d frames) from %d frames in", len(got), len(got)/2, want)
	}
	for i := 0; i < len(got)/2; i++ {
		var wl, wr int16
		if i < want {
			if channels == 1 {
				wl = int16(binary.LittleEndian.Uint16(pcm[i*2:]))
				wr = wl
			} else {
				wl = int16(binary.LittleEndian.Uint16(pcm[i*4:]))
				wr = int16(binary.LittleEndian.Uint16(pcm[i*4+2:]))
			}
		}
		if got[2*i] != wl || got[2*i+1] != wr {
			t.Fatalf("frame %d: got (%d, %d), want (%d, %d)", i, got[2*i], got[2*i+1], wl, wr)
		}
	}
}

// 9.1.1 sends a 9-byte chunk header; the current spec a 13-byte one with
// send_ahead. Either must find the payload.
func TestChunkPayloadFindsTheFlacFrameUnderEitherHeader(t *testing.T) {
	frame := []byte{0xFF, 0xF8, 0x69, 0x08}
	old := append([]byte{4, 0, 0, 0, 0, 0, 0, 0x30, 0x39}, frame...)
	ts, p, err := chunkPayload(old, "flac")
	if err != nil || ts != 12345 || p[0] != 0xFF {
		t.Fatalf("9-byte header: ts=%d payload=% x err=%v", ts, p, err)
	}
	cur := append([]byte{4, 0, 0, 0, 0, 0, 0, 0x30, 0x39, 0, 1, 0, 0}, frame...)
	ts, p, err = chunkPayload(cur, "flac")
	if err != nil || ts != 12345 || len(p) != len(frame) || p[0] != 0xFF {
		t.Fatalf("13-byte header: ts=%d payload=% x err=%v", ts, p, err)
	}
	if _, _, err := chunkPayload([]byte{4, 0, 0}, "flac"); err == nil {
		t.Error("a truncated header must be an error")
	}
}

// The device asks for exactly one format. Anything else in a stream/start
// is refused rather than played at the wrong speed.
func TestDecoderRefusesFormatsTheDeviceDidNotAskFor(t *testing.T) {
	for _, f := range []streamFormat{
		{Codec: "flac", SampleRate: 44100, Channels: 1, BitDepth: 16},
		{Codec: "flac", SampleRate: 48000, Channels: 1, BitDepth: 24},
		{Codec: "opus", SampleRate: 48000, Channels: 1, BitDepth: 16},
		{Codec: "flac", SampleRate: 48000, Channels: 6, BitDepth: 16},
	} {
		if _, err := newDecoder(f); !errors.Is(err, errFormat) {
			t.Errorf("%+v: want errFormat, got %v", f, err)
		}
	}
	if _, err := newDecoder(streamFormat{Codec: "flac", SampleRate: 48000, Channels: 1, BitDepth: 16, Header: "bm90IGZsYWM="}); err == nil {
		t.Error("a codec_header that is not a FLAC stream header must be refused")
	}
}

func TestPCMStereoKeepsBothChannels(t *testing.T) {
	d, err := newDecoder(streamFormat{Codec: "pcm", SampleRate: 48000, Channels: 2, BitDepth: 16})
	if err != nil {
		t.Fatal(err)
	}
	in := []byte{0x10, 0x27, 0xF0, 0xD8, 0x00, 0x10, 0x00, 0x30} // (10000,-10000), (4096,12288)
	got, _ := d.decode(nil, in)
	if want := []int16{10000, -10000, 4096, 12288}; !slices.Equal(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestPCMMonoIsPlayedOnBothChannels(t *testing.T) {
	d, err := newDecoder(streamFormat{Codec: "pcm", SampleRate: 48000, Channels: 1, BitDepth: 16})
	if err != nil {
		t.Fatal(err)
	}
	got, _ := d.decode(nil, []byte{0x10, 0x27, 0xF0, 0xD8}) // 10000, -10000
	if want := []int16{10000, 10000, -10000, -10000}; !slices.Equal(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

// supported_formats is in priority order, first preferred (spec; aiosendspin
// picks compatible[0]). Mono first unless a plug is in, and both listed
// either way, because stream/request-format may only name a listed format.
func TestHelloPrefersStereoOnlyWithAPlugIn(t *testing.T) {
	c := &Client{}
	for _, tc := range []struct {
		plug       bool
		first, alt int
	}{{false, 1, 2}, {true, 2, 1}} {
		c.SetStereo(tc.plug)
		f := c.hello().PlayerSupport.SupportedFormats
		if len(f) != 2 || f[0].Channels != tc.first || f[1].Channels != tc.alt {
			t.Errorf("plug %v: formats %+v, want %dch then %dch", tc.plug, f, tc.first, tc.alt)
		}
		for _, x := range f {
			if x.Codec != "flac" || x.SampleRate != outRate || x.BitDepth != outBits {
				t.Errorf("plug %v: format %+v is not one the decoder takes", tc.plug, x)
			}
			if _, err := newDecoder(streamFormat{Codec: x.Codec, SampleRate: x.SampleRate, Channels: x.Channels, BitDepth: x.BitDepth}); err != nil {
				t.Errorf("plug %v: advertised %+v but the decoder refuses it: %v", tc.plug, x, err)
			}
		}
	}
}
