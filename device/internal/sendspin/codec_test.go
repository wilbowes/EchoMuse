package sendspin

import (
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"math"
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

// The device asks for one rate and bit depth. Anything else in a stream/start
// is refused rather than played at the wrong speed.
func TestDecoderRefusesFormatsTheDeviceDidNotAskFor(t *testing.T) {
	for _, f := range []streamFormat{
		{Codec: "flac", SampleRate: 44100, Channels: 1, BitDepth: 16},
		{Codec: "flac", SampleRate: 48000, Channels: 1, BitDepth: 24},
		{Codec: "vorbis", SampleRate: 48000, Channels: 2, BitDepth: 16},
		{Codec: "opus", SampleRate: 44100, Channels: 2, BitDepth: 16},
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

// Stereo Opus first (a third of mono FLAC's bytes), mono FLAC for a server
// that cannot encode Opus. aiosendspin keeps the client's order.
func TestFormatsAskForStereoOpusThenMonoFlac(t *testing.T) {
	got := supportedFormats()
	want := []audioFormat{
		{Codec: "opus", Channels: 2, SampleRate: 48000, BitDepth: 16},
		{Codec: "flac", Channels: 1, SampleRate: 48000, BitDepth: 16},
	}
	if len(got) != len(want) {
		t.Fatalf("got %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("format %d: got %+v, want %+v", i, got[i], want[i])
		}
	}
}

// opusPackets reads the opus package's fixture: 1s of stereo, 1kHz left and
// 440Hz right at -12dBFS, encoded by libopus through PyAV as aiosendspin does.
func opusPackets(t *testing.T) [][]byte {
	t.Helper()
	b, err := os.ReadFile("../opus/testdata/tones.pk")
	if err != nil {
		t.Fatal(err)
	}
	var out [][]byte
	for len(b) >= 2 {
		n := int(binary.BigEndian.Uint16(b))
		out = append(out, b[2:2+n])
		b = b[2+n:]
	}
	return out
}

func opusChunk(header int, ts int64, sendAhead uint32, p []byte) []byte {
	m := make([]byte, header, header+len(p))
	m[0] = 4
	binary.BigEndian.PutUint64(m[1:9], uint64(ts))
	if header == 13 {
		binary.BigEndian.PutUint32(m[9:13], sendAhead)
	}
	return append(m, p...)
}

// Opus has no sync code, so the header length is read from byte 9: zero is
// send_ahead's top byte (13 bytes), anything else is the packet's TOC (9).
func TestChunkPayloadFindsTheOpusPacketUnderEitherHeader(t *testing.T) {
	p := opusPackets(t)[3]
	for _, c := range []struct {
		name      string
		header    int
		sendAhead uint32
	}{
		{"9.1.1, 9 bytes", 9, 0},
		{"10.x, 13 bytes, 250ms ahead", 13, 250_000},
		{"10.x, 13 bytes, nothing ahead", 13, 0},
		{"10.x, 13 bytes, 16.7s ahead", 13, 1<<24 - 1},
	} {
		ts, got, err := chunkPayload(opusChunk(c.header, 12345, c.sendAhead, p), "opus")
		if err != nil || ts != 12345 || !bytes.Equal(got, p) {
			t.Errorf("%s: ts=%d payload %d bytes (want %d), err=%v", c.name, ts, len(got), len(p), err)
		}
	}
	if _, _, err := chunkPayload(opusChunk(13, 1, 250_000, nil), "opus"); err == nil {
		t.Error("a 13-byte header with no packet must be an error")
	}
}

// The rule rests on no music packet having a TOC of zero (SILK narrowband
// 10ms mono). Holds for every packet libopus made for the fixture.
func TestNoOpusMusicPacketStartsWithZero(t *testing.T) {
	for i, p := range opusPackets(t) {
		if p[0] == 0 {
			t.Fatalf("packet %d has TOC 0, which the header rule reads as send_ahead", i)
		}
	}
}

// A whole stream through the session's decoder, as 10.x sends it: mono out,
// both sides' tones present at the average's level.
func TestOpusStreamDecodesToMono(t *testing.T) {
	d, err := newDecoder(streamFormat{Codec: "opus", SampleRate: 48000, Channels: 2, BitDepth: 16})
	if err != nil {
		t.Fatal(err)
	}
	defer d.close()
	var pcm []int16
	for i, p := range opusPackets(t) {
		_, payload, err := chunkPayload(opusChunk(13, int64(i)*20000, 250_000, p), "opus")
		if err != nil {
			t.Fatal(err)
		}
		if pcm, err = d.decode(pcm, payload); err != nil {
			t.Fatal(err)
		}
	}
	if len(pcm) < 48000 {
		t.Fatalf("%d mono samples, want at least 48000", len(pcm))
	}
	for _, hz := range []float64{1000, 440} {
		if got := toneDb(pcm[len(pcm)/4:len(pcm)*3/4], hz); math.Abs(got-(-18)) > 1 {
			t.Errorf("%gHz at %.1f dBFS, want -18 ±1 (each side at -12, averaged)", hz, got)
		}
	}
}

// reset and close are called on whatever the session holds, nil included.
func TestDecoderResetAndCloseAreSafe(t *testing.T) {
	var nilDec *decoder
	nilDec.reset()
	nilDec.close()
	d, err := newDecoder(streamFormat{Codec: "opus", SampleRate: 48000, Channels: 2, BitDepth: 16})
	if err != nil {
		t.Fatal(err)
	}
	d.reset()
	d.close()
	d.close()
	if _, err := d.decode(nil, opusPackets(t)[0]); err == nil {
		t.Error("decoded after close")
	}
	f, _ := newDecoder(streamFormat{Codec: "flac", SampleRate: 48000, Channels: 1, BitDepth: 16})
	f.reset()
	f.close()
}

// toneDb is the level of one frequency in mono pcm, by Goertzel.
func toneDb(pcm []int16, hz float64) float64 {
	w := 2 * math.Pi * hz / 48000
	c := 2 * math.Cos(w)
	var s1, s2 float64
	for _, v := range pcm {
		s := float64(v)/32768 + c*s1 - s2
		s2, s1 = s1, s
	}
	return 20 * math.Log10(math.Sqrt(s1*s1+s2*s2-c*s1*s2)/float64(len(pcm))*2)
}
