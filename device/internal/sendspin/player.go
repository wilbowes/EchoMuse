package sendspin

import (
	"math"
	"sync"
	"time"
)

// epoch anchors the client clock. time.Time carries a monotonic reading, so
// Sub between two of them is immune to the wall clock being stepped — which
// the controller does to every Echo on connect (time_ms on the ack).
var epoch = time.Now()

func nowUs() int64 { return time.Since(epoch).Microseconds() }

func toUs(t time.Time) int64 { return t.Sub(epoch).Microseconds() }

// Sync correction, after the spec's suggested strategy: whole-frame drops and
// repeats, bit-exact everywhere else.
const (
	// deadbandFrames: errors under ~100µs are left alone.
	deadbandFrames = 5
	// maxStepFrames per period: 8 of 2048 is 0.39% speed, inside the spec's
	// 0.5% cap. The DAC's own crystal runs -0.056% (1.15 frames a period,
	// measured 2026-08-10), so this keeps up with room to spare.
	maxStepFrames = 8
	// snapUs: past this the error is a disturbance rather than drift, and
	// one-shot resync beats a second of audible speed change.
	snapUs = 5000
)

type chunk struct {
	ts  int64   // server µs of the first frame
	pcm []int16 // ch channels interleaved
	ch  uint8   // 1, or 2 for a stereo chunk (#273)
}

// frames is the chunk's length in frames. Not a divide by ch: 32-bit ARM Go
// has no divide instruction, and this runs per chunk in Fill.
func (c chunk) frames() int {
	if c.ch == 2 {
		return len(c.pcm) / 2
	}
	return len(c.pcm)
}

// frame returns frame j's two sides as the speaker is to play them: a mono
// chunk on both, a stereo one as it is when stereo is allowed, else folded.
func (c chunk) frame(j int, stereo bool) (l, r int16) {
	if c.ch != 2 {
		return c.pcm[j], c.pcm[j]
	}
	l, r = c.pcm[2*j], c.pcm[2*j+1]
	if !stereo {
		m := fold(l, r)
		return m, m
	}
	return l, r
}

// fold is a stereo frame to mono, the exact expression and order the decoder
// used to average FLAC and PCM sides with before stereo existed, so folding a
// stereo chunk here plays the same bytes as decoding it to mono did.
func fold(l, r int16) int16 { return int16((int32(l) + int32(r)) / 2) }

// player holds decoded audio and hands it to the speaker at the right time.
//
// The speaker PULLS (Fill): it knows when the period it is building will
// reach the DAC, and only a pull at that moment can place a sample to the
// millisecond. The existing music plane is push-and-play-in-order, which is
// why Sendspin cannot simply feed it.
type player struct {
	mu      sync.Mutex
	q       []chunk
	off     int // frames consumed from q[0]
	playing bool

	filter  *timeFilter
	delayUs int64 // static_delay_ms, µs

	gainTarget, gain int32 // Q15

	st playerStats
}

// playerStats are counts since the stream started, for the status report.
type playerStats struct {
	Snaps       int `json:"snaps"`
	Corrections int `json:"corrections"`
	Underruns   int `json:"underruns"`
	LateDrops   int `json:"lateDrops"`
	LastErrorUs int `json:"lastErrorUs"`
}

func newPlayer(f *timeFilter) *player {
	return &player{filter: f, gainTarget: unity, gain: unity}
}

const unity int32 = 1 << 15

// volumeGain is the spec's perceptual curve: amplitude = (v/100)^1.5.
func volumeGain(volume int, muted bool) int32 {
	if muted || volume <= 0 {
		return 0
	}
	if volume >= 100 {
		return unity
	}
	return int32(math.Pow(float64(volume)/100, 1.5)*float64(unity) + 0.5)
}

func (p *player) setGain(volume int, muted bool) {
	p.mu.Lock()
	p.gainTarget = volumeGain(volume, muted)
	p.mu.Unlock()
}

func (p *player) setFilter(f *timeFilter) {
	p.mu.Lock()
	p.filter = f
	p.mu.Unlock()
}

func (p *player) setDelay(ms int) {
	p.mu.Lock()
	p.delayUs = int64(ms) * 1000
	p.mu.Unlock()
}

// push queues one decoded chunk of ch interleaved channels (1 or 2; anything
// else is taken as 1). A chunk that starts before the one already at the
// tail is a server that went backwards; it is taken as a restart. Mono and
// stereo chunks may follow each other in the queue (a plug change
// mid-stream): time is counted in frames, so they keep it.
func (p *player) push(ts int64, pcm []int16, ch int) {
	c := chunk{ts: ts, pcm: pcm, ch: 1}
	if ch == 2 {
		c.ch = 2
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if n := len(p.q); n > 0 && ts < p.q[n-1].ts {
		p.clearLocked()
	}
	p.q = append(p.q, c)
}

// clear drops everything buffered: stream/clear, stream/end, or HA taking
// the music plane.
func (p *player) clear() {
	p.mu.Lock()
	p.clearLocked()
	p.mu.Unlock()
}

func (p *player) clearLocked() {
	p.q, p.off, p.playing = nil, 0, false
}

func (p *player) resetStats() {
	p.mu.Lock()
	p.st = playerStats{}
	p.mu.Unlock()
}

func (p *player) stats() playerStats {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.st
}

// buffered is how much audio is queued, as time.
func (p *player) buffered() time.Duration {
	p.mu.Lock()
	defer p.mu.Unlock()
	n := -p.off
	for _, c := range p.q {
		n += c.frames()
	}
	return time.Duration(n) * time.Second / outRate
}

func (p *player) active() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.q) > 0
}

// Fill writes one period of stereo S16LE to out, whose first frame will
// reach the DAC at playAt. It returns false when there is nothing to play at
// that moment, so the speaker treats the music plane as empty.
//
// stereo is the speaker's word, for this period, on whether L may differ
// from R (#273): only with a plug known to be in the jack, since the
// internal speaker plays the right channel alone. Without it every stereo
// chunk is folded to mono, so a queue decoded while the plug was in plays
// mono from the next period after it comes out.
func (p *player) Fill(out []byte, playAt time.Time, stereo bool) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	frames := len(out) / 4
	if len(p.q) == 0 || !p.filter.synchronized() {
		if p.playing {
			p.st.Underruns++
			p.playing = false
		}
		return false
	}

	errUs := toUs(playAt) - p.localTimeLocked()
	errFrames := int(math.Round(float64(errUs) * outRate / 1e6))
	p.st.LastErrorUs = int(errUs)

	lead := 0 // frames of silence before the first sample
	repeat := 0
	switch {
	case !p.playing || errUs > snapUs || errUs < -snapUs:
		if errFrames > 0 {
			// Late: the first samples should already have played.
			if !p.dropLocked(errFrames) {
				p.playing = false
				return false
			}
		} else if errFrames < 0 {
			if -errFrames >= frames {
				p.playing = false
				return false // not due yet
			}
			lead = -errFrames
		}
		p.playing = true
		p.st.Snaps++
	case errFrames > deadbandFrames:
		if !p.dropLocked(min(errFrames, maxStepFrames)) {
			p.playing = false
			return false
		}
		p.st.Corrections++
	case errFrames < -deadbandFrames:
		repeat = min(-errFrames, maxStepFrames)
		p.st.Corrections++
	}

	start, end := p.gain, p.gainTarget
	p.gain = end
	gainAt := func(i int) int32 {
		if start == end {
			return end
		}
		return start + (end-start)*int32(i)/int32(frames)
	}

	i := 0
	for ; i < lead; i++ {
		put(out, i, 0, 0)
	}
	if repeat > 0 {
		l, r := p.q[0].frame(p.off, stereo)
		for k := 0; k < repeat && i < frames; k++ {
			g := gainAt(i)
			put(out, i, scale(l, g), scale(r, g))
			i++
		}
	}
	for i < frames {
		if len(p.q) == 0 {
			// Ran dry mid-period: the rest is silence, and the next Fill
			// resyncs from scratch.
			for ; i < frames; i++ {
				put(out, i, 0, 0)
			}
			p.playing = false
			p.st.Underruns++
			break
		}
		ch := p.q[0]
		c := ch.pcm
		n := min(ch.frames()-p.off, frames-i)
		switch {
		case ch.ch != 2:
			for k := 0; k < n; k++ {
				s := scale(c[p.off+k], gainAt(i+k))
				put(out, i+k, s, s)
			}
		case stereo:
			for k := 0; k < n; k++ {
				g := gainAt(i + k)
				j := 2 * (p.off + k)
				put(out, i+k, scale(c[j], g), scale(c[j+1], g))
			}
		default:
			// Folded BEFORE the gain, as the decoder averaged before it.
			for k := 0; k < n; k++ {
				j := 2 * (p.off + k)
				s := scale(fold(c[j], c[j+1]), gainAt(i+k))
				put(out, i+k, s, s)
			}
		}
		i += n
		p.advanceLocked(n)
	}
	return true
}

// localTimeLocked is when the next sample should reach the DAC on our clock.
func (p *player) localTimeLocked() int64 {
	c := p.q[0]
	ts := c.ts + int64(p.off)*1e6/outRate
	return p.filter.clientTime(ts) - p.delayUs
}

// dropLocked discards n frames, reporting false if the queue ran out first.
func (p *player) dropLocked(n int) bool {
	for n > 0 && len(p.q) > 0 {
		k := min(p.q[0].frames()-p.off, n)
		if k == p.q[0].frames()-p.off && p.off == 0 {
			p.st.LateDrops++
		}
		p.advanceLocked(k)
		n -= k
	}
	return len(p.q) > 0
}

func (p *player) advanceLocked(n int) {
	p.off += n
	for len(p.q) > 0 && p.off >= p.q[0].frames() {
		p.off -= p.q[0].frames()
		p.q[0].pcm = nil
		p.q = p.q[1:]
	}
}

func scale(s int16, g int32) int16 {
	if g == unity {
		return s
	}
	return int16((int32(s) * g) >> 15)
}

// put writes stereo frame i.
func put(out []byte, i int, l, r int16) {
	ul, ur := uint16(l), uint16(r)
	out[i*4], out[i*4+1] = byte(ul), byte(ul>>8)
	out[i*4+2], out[i*4+3] = byte(ur), byte(ur>>8)
}
