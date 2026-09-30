package sendspin

import (
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"os"
	"testing"
)

// Chunks from aiosendspin's own FlacEncoder (testdata/gen_flac.py), which is
// what Music Assistant streams with. FLAC is lossless, so the decode must be
// bit-exact against the PCM that went in.
func TestFlacChunksFromTheServerEncoderDecodeBitExact(t *testing.T) {
	raw, err := os.ReadFile("testdata/flac_chunks.json")
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
	d, err := newDecoder(streamFormat{Codec: "flac", SampleRate: 48000, Channels: 1, BitDepth: 16, Header: fx.Header})
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
	want := len(pcm) / 2
	// The encoder pads its last frame out to the block size on flush.
	if len(got) < want || len(got) > want+4608 {
		t.Fatalf("decoded %d samples from %d in", len(got), want)
	}
	for i, s := range got {
		w := int16(0)
		if i < want {
			w = int16(binary.LittleEndian.Uint16(pcm[i*2:]))
		}
		if s != w {
			t.Fatalf("sample %d: got %d, want %d", i, s, w)
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

func TestPCMStereoIsDownmixed(t *testing.T) {
	d, err := newDecoder(streamFormat{Codec: "pcm", SampleRate: 48000, Channels: 2, BitDepth: 16})
	if err != nil {
		t.Fatal(err)
	}
	in := []byte{0x10, 0x27, 0xF0, 0xD8, 0x00, 0x10, 0x00, 0x30} // (10000,-10000), (4096,12288)
	got, _ := d.decode(nil, in)
	if len(got) != 2 || got[0] != 0 || got[1] != 8192 {
		t.Errorf("got %v, want [0 8192]", got)
	}
}
