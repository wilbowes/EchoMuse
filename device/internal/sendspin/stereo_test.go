package sendspin

import (
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"math"
	"os"
	"slices"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/opus"
)

// Stereo while a plug is in the jack (#273): the decoder keeps a stereo
// stream's sides only when hinted, and the player lets L != R through only
// when the speaker's period allows it.

var opusStereo = streamFormat{Codec: "opus", SampleRate: 48000, Channels: 2, BitDepth: 16}

// libopusDecode is the reference: one libopus decoder of the given channels
// over the whole fixture, each packet's output kept apart.
func libopusDecode(t *testing.T, channels int) [][]int16 {
	t.Helper()
	d, err := opus.NewDecoder(channels)
	if err != nil {
		t.Fatal(err)
	}
	defer d.Close()
	buf := make([]int16, opus.MaxFrame*channels)
	var out [][]int16
	for _, p := range opusPackets(t) {
		n, err := d.Decode(p, buf)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, append([]int16(nil), buf[:n*channels]...))
	}
	return out
}

// sessionDecode runs the fixture through the session's decoder as onAudio
// does, the hint for packet i being hint(i), and returns each packet's
// samples and channels.
func sessionDecode(t *testing.T, d *decoder, hint func(i int) bool) ([][]int16, []int) {
	t.Helper()
	var out [][]int16
	var chs []int
	for i, p := range opusPackets(t) {
		d.setStereo(hint(i))
		pcm, ch, err := d.decode(nil, p, hint(i))
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, pcm)
		chs = append(chs, ch)
	}
	return out, chs
}

func maxDiff(a, b []int16) int {
	if len(a) != len(b) {
		return math.MaxInt
	}
	worst := 0
	for i := range a {
		d := int(a[i]) - int(b[i])
		if d < 0 {
			d = -d
		}
		worst = max(worst, d)
	}
	return worst
}

// Unplugged, the session's decoder is libopus's 1-channel decoder, to the
// bit: decoding stereo and averaging is not the same (1 LSB on a quarter of
// tones.pk), so mono stays what it was.
func TestOpusUnpluggedEqualsLibopusMono(t *testing.T) {
	d, err := newDecoder(opusStereo)
	if err != nil {
		t.Fatal(err)
	}
	defer d.close()
	got, chs := sessionDecode(t, d, func(int) bool { return false })
	want := libopusDecode(t, 1)
	for i := range want {
		if chs[i] != 1 || !slices.Equal(got[i], want[i]) {
			t.Fatalf("packet %d: %dch, differs from libopus mono by %d", i, chs[i], maxDiff(got[i], want[i]))
		}
	}
}

// Plugged from the start, each side keeps its own tone: 1kHz left and 440Hz
// right at -12dBFS, and neither in the other channel.
func TestOpusStereoKeepsEachSide(t *testing.T) {
	d, err := newDecoder(opusStereo)
	if err != nil {
		t.Fatal(err)
	}
	defer d.close()
	got, chs := sessionDecode(t, d, func(int) bool { return true })
	var l, r []int16
	for i, p := range got {
		if chs[i] != 2 {
			t.Fatalf("packet %d: %dch", i, chs[i])
		}
		for j := 0; j+1 < len(p); j += 2 {
			l, r = append(l, p[j]), append(r, p[j+1])
		}
	}
	mid := func(s []int16) []int16 { return s[len(s)/4 : len(s)*3/4] }
	for _, c := range []struct {
		name     string
		s        []int16
		own, not float64
	}{{"left", l, 1000, 440}, {"right", r, 440, 1000}} {
		if got := toneDb(mid(c.s), c.own); math.Abs(got-(-12)) > 1 {
			t.Errorf("%s: %gHz at %.1fdBFS, want -12", c.name, c.own, got)
		}
		if got := toneDb(mid(c.s), c.not); got > -40 {
			t.Errorf("%s: the other side's %gHz at %.1fdBFS", c.name, c.not, got)
		}
	}
}

// A plug change mid-stream swaps libopus decoders, the new one fed the last
// opusReplay packets first. From the swap on, it comes within 1 LSB of a
// decoder that had run that way from the start, both ways, wherever the swap
// falls; within the first opusReplay packets the replay is the whole stream
// and the result is exact.
func TestOpusSwapAfterReplayIsWithinOneLSB(t *testing.T) {
	mono, stereo := libopusDecode(t, 1), libopusDecode(t, 2)
	n := len(mono)
	for _, toStereo := range []bool{true, false} {
		want := mono
		if toStereo {
			want = stereo
		}
		worstAll := 0
		for k := 1; k < n; k++ {
			d, err := newDecoder(opusStereo)
			if err != nil {
				t.Fatal(err)
			}
			got, _ := sessionDecode(t, d, func(i int) bool { return (i >= k) == toStereo })
			d.close()
			worst := 0
			for i := k; i < n; i++ {
				worst = max(worst, maxDiff(got[i], want[i]))
			}
			if k <= opusReplay && worst != 0 {
				t.Errorf("toStereo=%v swap at %d: %d LSB, want exact (the replay is the whole stream)", toStereo, k, worst)
			}
			if worst > 1 {
				t.Errorf("toStereo=%v swap at packet %d: %d LSB off a decoder run that way from the start", toStereo, k, worst)
			}
			worstAll = max(worstAll, worst)
		}
		t.Logf("toStereo=%v: worst %d LSB over every swap point", toStereo, worstAll)
	}
}

// At a stream's start there is nothing to replay, and nothing is missed.
func TestSwapAtStreamStartIsExact(t *testing.T) {
	d, err := newDecoder(opusStereo)
	if err != nil {
		t.Fatal(err)
	}
	defer d.close()
	got, _ := sessionDecode(t, d, func(int) bool { return true })
	for i, w := range libopusDecode(t, 2) {
		if !slices.Equal(got[i], w) {
			t.Fatalf("packet %d differs from libopus stereo by %d", i, maxDiff(got[i], w))
		}
	}
}

// stream/clear resets the decoder, and its history with it: a swap after the
// cut is fed nothing from before it, and decodes as a fresh decoder would.
func TestResetClearsHistory(t *testing.T) {
	pk := opusPackets(t)
	d, err := newDecoder(opusStereo)
	if err != nil {
		t.Fatal(err)
	}
	defer d.close()
	for _, p := range pk[:30] {
		if _, _, err := d.decode(nil, p, false); err != nil {
			t.Fatal(err)
		}
	}
	if d.histN != opusReplay {
		t.Fatalf("history holds %d packets after 30, want %d", d.histN, opusReplay)
	}
	d.reset()
	if d.histN != 0 {
		t.Fatalf("history holds %d packets after reset", d.histN)
	}
	ref, err := opus.NewDecoder(2)
	if err != nil {
		t.Fatal(err)
	}
	defer ref.Close()
	buf := make([]int16, opus.MaxFrame*2)
	for i, p := range pk[30:] {
		d.setStereo(true)
		got, ch, err := d.decode(nil, p, true)
		if err != nil {
			t.Fatal(err)
		}
		n, _ := ref.Decode(p, buf)
		if ch != 2 || !slices.Equal(got, buf[:n*2]) {
			t.Fatalf("packet %d after the cut: %dch, %d LSB off a fresh decoder", 30+i, ch, maxDiff(got, buf[:n*2]))
		}
	}
}

type flacFixture struct {
	Header string
	Chunks []struct{ Data string }
	PCM    string
}

func readFlacFixture(t *testing.T, path string) (flacFixture, [][]byte, []int16) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fx flacFixture
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	var chunks [][]byte
	for _, c := range fx.Chunks {
		b, err := base64.StdEncoding.DecodeString(c.Data)
		if err != nil {
			t.Fatal(err)
		}
		chunks = append(chunks, b)
	}
	b, _ := base64.StdEncoding.DecodeString(fx.PCM)
	pcm := make([]int16, len(b)/2)
	for i := range pcm {
		pcm[i] = int16(binary.LittleEndian.Uint16(b[i*2:]))
	}
	return fx, chunks, pcm
}

// aiosendspin's own encoder on two different channels (gen_flac_stereo.py),
// so its inter-channel modes are in play: kept, both sides come back bit
// exact; not kept, the average is today's integer one.
func TestFlacStereoChunksDecodeBitExact(t *testing.T) {
	fx, chunks, pcm := readFlacFixture(t, "testdata/flac_chunks_stereo.json")
	for _, keep := range []bool{true, false} {
		d, err := newDecoder(streamFormat{Codec: "flac", SampleRate: 48000, Channels: 2, BitDepth: 16, Header: fx.Header})
		if err != nil {
			t.Fatal(err)
		}
		var got []int16
		for i, c := range chunks {
			var ch int
			if got, ch, err = d.decode(got, c, keep); err != nil {
				t.Fatalf("chunk %d: %v", i, err)
			}
			if want := map[bool]int{true: 2, false: 1}[keep]; ch != want {
				t.Fatalf("keep=%v chunk %d: %dch, want %d", keep, i, ch, want)
			}
		}
		frames := len(pcm) / 2
		ch := map[bool]int{true: 2, false: 1}[keep]
		if len(got)/ch < frames || len(got)/ch > frames+4608 {
			t.Fatalf("keep=%v: decoded %d frames from %d", keep, len(got)/ch, frames)
		}
		for i := 0; i < frames; i++ {
			l, r := pcm[2*i], pcm[2*i+1]
			if keep {
				if got[2*i] != l || got[2*i+1] != r {
					t.Fatalf("frame %d: got (%d, %d), want (%d, %d)", i, got[2*i], got[2*i+1], l, r)
				}
			} else if w := int16((int32(l) + int32(r)) / 2); got[i] != w {
				t.Fatalf("frame %d: mono %d, want %d", i, got[i], w)
			}
		}
	}
}

func TestPCMStereoKeepsSides(t *testing.T) {
	d, err := newDecoder(streamFormat{Codec: "pcm", SampleRate: 48000, Channels: 2, BitDepth: 16})
	if err != nil {
		t.Fatal(err)
	}
	in := []byte{0x10, 0x27, 0xF0, 0xD8, 0x00, 0x10, 0x00, 0x30} // (10000,-10000), (4096,12288)
	got, ch, _ := d.decode(nil, in, true)
	if want := []int16{10000, -10000, 4096, 12288}; ch != 2 || !slices.Equal(got, want) {
		t.Errorf("got %v (%dch), want %v", got, ch, want)
	}
	// A mono stream has one side to keep.
	m, _ := newDecoder(streamFormat{Codec: "pcm", SampleRate: 48000, Channels: 1, BitDepth: 16})
	got, ch, _ = m.decode(nil, in[:4], true)
	if want := []int16{10000, -10000}; ch != 1 || !slices.Equal(got, want) {
		t.Errorf("mono stream: got %v (%dch), want %v", got, ch, want)
	}
}

// stereoRamp is n stereo frames that say where they came from: the left
// counts up and the right is half of it, so a fold is (3v/2)/2 and a frame
// that loses its pairing shows.
func stereoRamp(start, n int) []int16 {
	s := make([]int16, 2*n)
	for i := 0; i < n; i++ {
		v := int16((start + i) % 30000)
		s[2*i], s[2*i+1] = v, v/2
	}
	return s
}

func sampleR(out []byte, i int) int16 { return int16(binary.LittleEndian.Uint16(out[i*4+2:])) }

// playSchedule pulls periods from p on goldenPlay's fixed clock (a snap, a
// repeat, a drop, a second snap, a volume ramp) until the queue runs dry,
// handing each period to check. stereo says what each period is told.
func playSchedule(p *player, stereo func(k int) bool, check func(k int, played bool, out []byte)) {
	out := make([]byte, periodFrames*4)
	periodUs := int64(periodFrames) * 1_000_000 / outRate
	playAt := int64(5_000_000)
	for k := 0; k < 400; k++ {
		us := playAt
		switch k {
		case 3:
			us -= 40 * 1_000_000 / outRate
		case 6:
			us += 40 * 1_000_000 / outRate
		case 9:
			us += 20_000
		}
		switch k {
		case 5:
			p.setGain(50, false)
		case 8:
			p.setGain(80, false)
		case 12:
			p.setGain(100, false)
		}
		played := p.Fill(out, at(us), stereo(k))
		check(k, played, out)
		if !played && !p.active() {
			return
		}
		playAt += periodUs
	}
}

// A stereo FLAC stream decoded with its sides kept and folded by Fill plays
// the same bytes, period for period, as the same stream decoded to mono:
// the fold is the decoder's old average, before the gain.
func TestFillFoldOfStereoFlacEqualsMonoDecode(t *testing.T) {
	fx, chunks, _ := readFlacFixture(t, "testdata/flac_chunks_stereo.json")
	f := streamFormat{Codec: "flac", SampleRate: 48000, Channels: 2, BitDepth: 16, Header: fx.Header}
	queue := func(keep bool) *player {
		d, err := newDecoder(f)
		if err != nil {
			t.Fatal(err)
		}
		p := newPlayer(syncedFilter(0))
		frames := 0
		for _, c := range chunks {
			pcm, ch, err := d.decode(nil, c, keep)
			if err != nil {
				t.Fatal(err)
			}
			p.push(5_000_000+int64(frames)*1_000_000/outRate, pcm, ch)
			frames += len(pcm) / ch
		}
		return p
	}
	var folded, mono [][]byte
	record := func(dst *[][]byte) func(int, bool, []byte) {
		return func(_ int, played bool, out []byte) {
			if played {
				*dst = append(*dst, append([]byte(nil), out...))
			} else {
				*dst = append(*dst, nil)
			}
		}
	}
	never := func(int) bool { return false }
	pf, pm := queue(true), queue(false)
	playSchedule(pf, never, record(&folded))
	playSchedule(pm, never, record(&mono))
	if st := pf.stats(); st.Corrections < 2 || st.Snaps < 2 {
		t.Fatalf("schedule reached corrections=%d snaps=%d", st.Corrections, st.Snaps)
	}
	if len(folded) != len(mono) {
		t.Fatalf("%d periods folded, %d mono", len(folded), len(mono))
	}
	for k := range mono {
		if string(folded[k]) != string(mono[k]) {
			t.Fatalf("period %d: the fold differs from the mono decode", k)
		}
	}
}

// Told mono, Fill writes L == R in every frame, whatever is queued: mono and
// stereo chunks mixed, through a repeat, a drop, a snap and an underrun.
func TestFillNeverEmitsLNotEqualRWhenToldMono(t *testing.T) {
	p := newPlayer(syncedFilter(0))
	frames := 0
	for c := 0; c < 40; c++ {
		n := 1200
		var pcm []int16
		ch := 1 + c&1
		if ch == 2 {
			pcm = stereoRamp(frames, n)
		} else {
			pcm = ramp(frames, n)
		}
		p.push(5_000_000+int64(frames)*1_000_000/outRate, pcm, ch)
		frames += n
	}
	periods := 0
	playSchedule(p, func(int) bool { return false }, func(k int, played bool, out []byte) {
		if !played {
			return
		}
		periods++
		for i := 0; i < periodFrames; i++ {
			if sample(out, i) != sampleR(out, i) {
				t.Fatalf("period %d frame %d: L %d R %d", k, i, sample(out, i), sampleR(out, i))
			}
		}
	})
	if st := p.stats(); periods < 20 || st.Corrections < 2 || st.Snaps < 2 || st.Underruns < 1 {
		t.Fatalf("periods=%d %+v: the schedule did not reach every path", periods, st)
	}
}

// The speaker's answer takes effect at the next period, both ways, on audio
// already queued: stereo keeps the sides, mono folds them, stereo again keeps
// them, and no frame is lost or repeated across the changes.
func TestFillSwitchesAtTheNextPeriodBothWays(t *testing.T) {
	p := newPlayer(syncedFilter(0))
	p.push(5_000_000, stereoRamp(0, 48000), 2)
	out := make([]byte, periodFrames*4)
	periodUs := int64(periodFrames) * 1_000_000 / outRate
	next := 0
	for k, st := range []bool{true, false, true, false} {
		if !p.Fill(out, at(5_000_000+int64(k)*periodUs), st) {
			t.Fatalf("period %d: nothing played", k)
		}
		for i := 0; i < periodFrames; i++ {
			v := int16(next + i)
			wl, wr := v, v/2
			if !st {
				wl = int16((int32(v) + int32(v/2)) / 2)
				wr = wl
			}
			if sample(out, i) != wl || sampleR(out, i) != wr {
				t.Fatalf("period %d (stereo %v) frame %d: (%d, %d), want (%d, %d)",
					k, st, i, sample(out, i), sampleR(out, i), wl, wr)
			}
		}
		next += periodFrames
	}
}

// Mono and stereo chunks in one queue (a plug change mid-stream) count time
// in frames: the buffer reads true, and a period spanning both plays every
// frame once, in order.
func TestMixedQueueKeepsTime(t *testing.T) {
	p := newPlayer(syncedFilter(0))
	const n = 1000
	us := func(frames int) int64 { return 5_000_000 + int64(frames)*1_000_000/outRate }
	p.push(us(0), ramp(0, n), 1)
	p.push(us(n), stereoRamp(n, n), 2)
	p.push(us(2*n), ramp(2*n, n), 1)
	if got, want := p.buffered(), 3*n*1_000_000/outRate; got.Microseconds() != int64(want) {
		t.Fatalf("buffered %v, want %dus", got, want)
	}
	out := make([]byte, periodFrames*4)
	if !p.Fill(out, at(us(0)), true) {
		t.Fatal("nothing played")
	}
	for i := 0; i < periodFrames; i++ {
		v := int16(i)
		wr := v
		if i >= n && i < 2*n {
			wr = v / 2
		}
		if sample(out, i) != v || sampleR(out, i) != wr {
			t.Fatalf("frame %d: (%d, %d), want (%d, %d)", i, sample(out, i), sampleR(out, i), v, wr)
		}
	}
	// The next period starts on the frame after, in the last chunk.
	if !p.Fill(out, at(us(periodFrames)), true) {
		t.Fatal("second period: nothing played")
	}
	if got := sample(out, 0); got != periodFrames {
		t.Errorf("second period starts at %d, want %d", got, periodFrames)
	}
}
