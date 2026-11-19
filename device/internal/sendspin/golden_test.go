package sendspin

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"flag"
	"math"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// What the player handed the speaker before it could keep two channels
// apart (#273), pinned by hash: Opus, FLAC and stereo PCM decoded, queued and
// pulled period by period on a fixed clock that takes the player through a
// snap at the start, a repeat, a drop, a second snap, a volume ramp and the
// queue running dry. The hello is pinned with it, since what the device asks
// a server for is part of what it plays.
//
// Generated once, before the stereo path was written, and not to be
// regenerated to make a change pass: a moved hash means the mono output moved.
// Everything here is integer arithmetic and libopus's fixed point, so the
// hashes hold on every architecture.
var updateGolden = flag.Bool("update-golden", false, "rewrite testdata/golden_main.json")

type goldenFile struct {
	Opus      string `json:"opus"`
	Flac      string `json:"flac"`
	PCMStereo string `json:"pcmStereo"`
	Hello     string `json:"hello"`
}

// The only places this file touches the decoder and player API, so a change
// of signature changes these and nothing below them.

type goldenChunk struct {
	pcm    []int16
	frames int
	ch     int
}

func goldenDecode(t *testing.T, d *decoder, payload []byte) goldenChunk {
	t.Helper()
	pcm, ch, err := d.decode(nil, payload, false)
	if err != nil {
		t.Fatal(err)
	}
	return goldenChunk{pcm: pcm, frames: len(pcm) / ch, ch: ch}
}

func goldenPush(p *player, ts int64, c goldenChunk) { p.push(ts, c.pcm, c.ch) }

func goldenFill(p *player, out []byte, playAt time.Time) bool { return p.Fill(out, playAt, false) }

// goldenPlay queues every payload, stamped back to back from 5s on the
// server's clock, and pulls periods until the queue has run dry. It returns
// the hash of every period and whether each played, and the player's counts,
// so a caller can check the schedule reached what it is meant to.
func goldenPlay(t *testing.T, f streamFormat, payloads [][]byte) (string, playerStats) {
	t.Helper()
	d, err := newDecoder(f)
	if err != nil {
		t.Fatal(err)
	}
	defer d.close()
	p := newPlayer(syncedFilter(0))
	ts := int64(5_000_000)
	frames := 0
	for _, pl := range payloads {
		c := goldenDecode(t, d, pl)
		goldenPush(p, ts+int64(frames)*1_000_000/outRate, c)
		frames += c.frames
	}

	h := sha256.New()
	out := make([]byte, periodFrames*4)
	periodUs := int64(periodFrames) * 1_000_000 / outRate
	playAt := ts
	for k := 0; k < 400; k++ {
		us := playAt
		switch k {
		case 3:
			us -= 40 * 1_000_000 / outRate // 40 frames early: a repeat
		case 6:
			us += 40 * 1_000_000 / outRate // 40 frames late: a drop
		case 9:
			us += 20_000 // a 20ms jump: a snap
		}
		switch k {
		case 5:
			p.setGain(50, false)
		case 8:
			p.setGain(80, false)
		case 12:
			p.setGain(100, false)
		}
		for i := range out {
			out[i] = 0xAA // a stale byte left unwritten shows
		}
		played := goldenFill(p, out, at(us))
		if played {
			h.Write([]byte{1})
			h.Write(out)
		} else {
			h.Write([]byte{0})
		}
		if !played && !p.active() {
			break
		}
		playAt += periodUs
	}
	return hex.EncodeToString(h.Sum(nil)), p.stats()
}

func goldenOpus(t *testing.T) (string, playerStats) {
	return goldenPlay(t, streamFormat{Codec: "opus", SampleRate: 48000, Channels: 2, BitDepth: 16}, opusPackets(t))
}

func goldenFlac(t *testing.T) (string, playerStats) {
	raw, err := os.ReadFile("testdata/flac_chunks.json")
	if err != nil {
		t.Fatal(err)
	}
	var fx struct {
		Header string
		Chunks []struct{ Data string }
	}
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	var payloads [][]byte
	for _, c := range fx.Chunks {
		b, err := base64.StdEncoding.DecodeString(c.Data)
		if err != nil {
			t.Fatal(err)
		}
		payloads = append(payloads, b)
	}
	return goldenPlay(t, streamFormat{Codec: "flac", SampleRate: 48000, Channels: 1, BitDepth: 16, Header: fx.Header}, payloads)
}

// goldenStereoPCM is 1s of two different tones, odd sums included, in 25ms
// chunks: the side average is all of what it tests.
func goldenStereoPCM() [][]byte {
	var out [][]byte
	const chunk = 1200
	for c := 0; c < 40; c++ {
		b := make([]byte, chunk*4)
		for i := 0; i < chunk; i++ {
			n := float64(c*chunk + i)
			l := int16(12000*math.Sin(2*math.Pi*440*n/48000)) | 1
			r := int16(-9000 * math.Sin(2*math.Pi*1000*n/48000))
			binary.LittleEndian.PutUint16(b[i*4:], uint16(l))
			binary.LittleEndian.PutUint16(b[i*4+2:], uint16(r))
		}
		out = append(out, b)
	}
	return out
}

func goldenHello(t *testing.T) string {
	c, err := New(Config{
		StorePath: filepath.Join(t.TempDir(), "s.json"),
		Name:      "Echo Golden",
		Instance:  "echomuse-golden",
		Product:   "EchoMuse biscuit",
		Version:   "v0.0.0-golden",
	})
	if err != nil {
		t.Fatal(err)
	}
	b, err := marshalMsg("client/hello", c.hello())
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func TestMonoPlaybackMatchesGolden(t *testing.T) {
	var got goldenFile
	var st playerStats
	got.Opus, st = goldenOpus(t)
	// The schedule must reach every path it is there for, or the hash pins
	// less than it claims.
	if st.Snaps < 2 || st.Corrections < 2 || st.Underruns < 1 {
		t.Fatalf("opus: schedule reached snaps=%d corrections=%d underruns=%d", st.Snaps, st.Corrections, st.Underruns)
	}
	got.Flac, st = goldenFlac(t)
	if st.Snaps < 2 || st.Corrections < 2 || st.Underruns < 1 {
		t.Fatalf("flac: schedule reached snaps=%d corrections=%d underruns=%d", st.Snaps, st.Corrections, st.Underruns)
	}
	f := streamFormat{Codec: "pcm", SampleRate: 48000, Channels: 2, BitDepth: 16}
	got.PCMStereo, st = goldenPlay(t, f, goldenStereoPCM())
	if st.Snaps < 2 || st.Corrections < 2 || st.Underruns < 1 {
		t.Fatalf("pcm: schedule reached snaps=%d corrections=%d underruns=%d", st.Snaps, st.Corrections, st.Underruns)
	}
	got.Hello = goldenHello(t)

	// What the hello declares, in the open, beside its hash.
	if bufferCapacity != 576000 {
		t.Errorf("bufferCapacity %d, want 576000", bufferCapacity)
	}

	path := filepath.Join("testdata", "golden_main.json")
	if *updateGolden {
		b, err := json.MarshalIndent(got, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("wrote %s", path)
		return
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var want goldenFile
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatal(err)
	}
	if got.Opus != want.Opus {
		t.Error("opus: playback moved")
	}
	if got.Flac != want.Flac {
		t.Error("flac: playback moved")
	}
	if got.PCMStereo != want.PCMStereo {
		t.Error("stereo pcm: playback moved")
	}
	if got.Hello != want.Hello {
		t.Error("client/hello moved")
	}
}
