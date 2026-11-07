// Package echoref aligns a playback loopback that arrives on its own PCM with
// the microphones, so it can be the echo canceller's far-end reference.
//
// On the Dot 2 and the radar the loopback is a channel of the microphone
// frame and needs no aligning. The Dot 3's (DL1_AWB_Record, 48 kHz stereo) is
// a separate stream: its samples line up with the microphones' by count
// until the driver slips a period (16 ms, measured on 41C8, 2026-11-07), and
// the offset between the two counts is set wherever each stream happened to
// start. So the offset is measured from the audio itself: while something
// plays, the microphones carry its echo, and a cross-correlation against the
// reference says how far apart they are. Between slips that delay held to
// half a millisecond, so a slow search (once a second, decimated first) is
// enough, and the reference is shifted only when two searches in a row agree
// that it moved.
//
// Nothing is returned until a search has locked: an unaligned reference is
// worse than none, and the caller falls back to the software tap.
package echoref

import (
	"encoding/binary"
	"math"
	"sync"
	"time"
)

const (
	// Rate is the reference's and the microphones' rate after decimation.
	Rate = 16000

	ringLen = 4 * Rate // 4 s of reference

	// Lead is how far the returned reference runs ahead of the echo it
	// cancels: the canceller's filter is causal, so the echo must land a
	// little inside it, never before it.
	Lead = 64 // 4 ms

	// window is how much microphone audio one search compares.
	window = Rate // 1 s

	// coarseMax / lockedMax bound the delay searched: a stream start can put
	// the two counts anywhere within a chunk or two, a slip moves them by a
	// period.
	coarseMax = Rate / 4  // ±250 ms when not locked
	lockedMax = Rate / 25 // ±40 ms when locked

	decim = 8 // coarse search at 2 kHz

	// minCorr is the normalised correlation a search must reach to count;
	// speech through 41C8 reached 0.7-0.9.
	minCorr = 0.4

	// maxMissRun is how many searches in a row over audio may find nothing
	// before the lock is dropped.
	maxMissRun = 3

	// takeWait bounds how long Take waits for a reference that is about to
	// arrive: a few loopback periods.
	takeWait = 40 * time.Millisecond

	// minRefRMS skips searches over silence (int16 units, ~-50 dBFS).
	minRefRMS = 100.0
)

// Aligner holds the decimated reference and the offset that maps a
// microphone sample index to a reference sample index.
type Aligner struct {
	mu   sync.Mutex
	ring [ringLen]int16
	head int64 // reference samples ever written

	dec decimator

	// smu guards the search state below: Observe runs on the capture
	// loop, Take on the cancelling one.
	smu         sync.Mutex
	micBuf      []int16 // the last window of microphone audio, for a search
	micEnd      int64   // microphone index just past micBuf
	offset      int64   // reference index = microphone index + offset
	locked      bool
	pending     int64 // a moved offset seen once, waiting for a second search
	missRun     int   // searches in a row over audio that found nothing
	havePend    bool
	sinceSearch int

	// Counters for the caller's log line.
	Searches, Locks, Moves, Misses, Losses, Late int
}

// New returns an Aligner for a 48 kHz stereo S16 loopback.
func New() *Aligner {
	return &Aligner{dec: newDecimator(), micBuf: make([]int16, 0, window+2*coarseMax+Rate)}
}

// PushLoopback takes raw 48 kHz interleaved stereo S16LE and adds its left
// channel, decimated to 16 kHz, to the reference.
func (a *Aligner) PushLoopback(raw []byte) {
	out := a.dec.run(raw)
	a.mu.Lock()
	for _, s := range out {
		a.ring[a.head%ringLen] = s
		a.head++
	}
	a.mu.Unlock()
}

// Locked reports whether a search has found the delay.
func (a *Aligner) Locked() bool {
	a.smu.Lock()
	defer a.smu.Unlock()
	return a.locked
}

// Take returns the reference for the n microphone samples that start at
// micIndex, as S16LE, or nil when it is not locked. Locked but not yet in
// the ring (the loopback arrives a period at a time, the microphones a batch
// at a time) waits up to takeWait for it; gone from the ring is nil.
func (a *Aligner) Take(micIndex int64, n int) []byte {
	if !a.Locked() {
		return nil
	}
	start := micIndex + a.Offset()
	deadline := time.Now().Add(takeWait)
	a.mu.Lock()
	defer a.mu.Unlock()
	for start+int64(n) > a.head {
		if time.Now().After(deadline) {
			a.Late++
			return nil
		}
		a.mu.Unlock()
		time.Sleep(2 * time.Millisecond)
		a.mu.Lock()
	}
	if start < a.head-ringLen {
		return nil
	}
	out := make([]byte, 2*n)
	for i := 0; i < n; i++ {
		binary.LittleEndian.PutUint16(out[2*i:], uint16(a.ring[(start+int64(i))%ringLen]))
	}
	return out
}

// Observe feeds the search with microphone audio, before any cancellation,
// at its position in the stream. Called for every batch whether or not
// anything is cancelling, so the lock is already held when a turn starts.
func (a *Aligner) Observe(micIndex int64, mic []int16) {
	a.smu.Lock()
	defer a.smu.Unlock()
	a.observe(micIndex, mic)
}

func (a *Aligner) observe(micIndex int64, mic []int16) {
	if micIndex != a.micEnd {
		a.micBuf = a.micBuf[:0] // a gap in the microphone stream: start over
	}
	a.micBuf = append(a.micBuf, mic...)
	if keep := window + 2*coarseMax; len(a.micBuf) > keep {
		a.micBuf = append(a.micBuf[:0], a.micBuf[len(a.micBuf)-keep:]...)
	}
	a.micEnd = micIndex + int64(len(mic))
	a.sinceSearch += len(mic)
	if len(a.micBuf) < window || a.sinceSearch < Rate {
		return
	}
	a.sinceSearch = 0
	a.search()
}

// search measures the delay of the echo in micBuf behind the reference and
// moves the offset if it has changed.
func (a *Aligner) search() {
	max := int64(coarseMax)
	centre := int64(0) // reference index - microphone index
	if a.locked {
		max = lockedMax
		centre = a.offset - Lead
	}
	a.mu.Lock()
	head := a.head
	// The newest window of microphone audio whose whole search span is
	// already in the reference: the two counts advance together, so the
	// span past a window ending now has not arrived yet.
	micStart := a.micEnd - window
	if late := micStart + window + centre + max - head; late > 0 {
		micStart -= late
	}
	bufStart := a.micEnd - int64(len(a.micBuf))
	lo := micStart + centre - max
	hi := micStart + window + centre + max
	if micStart < bufStart || lo < head-ringLen || hi > head {
		a.mu.Unlock()
		return
	}
	mic := a.micBuf[micStart-bufStart : micStart-bufStart+window]
	ref := make([]int16, hi-lo)
	for i := range ref {
		ref[i] = a.ring[(lo+int64(i))%ringLen]
	}
	a.mu.Unlock()
	if rms(ref) < minRefRMS {
		return
	}
	a.Searches++
	lag, corr := xcorrPeak(mic, ref, int(max))
	if corr < minCorr {
		a.Misses++
		// A jump past the locked search, or a room that changed: an
		// unaligned reference is worse than none, so let go and search wide.
		if a.missRun++; a.locked && a.missRun >= maxMissRun {
			a.locked, a.havePend = false, false
			a.Losses++
		}
		return
	}
	a.missRun = 0
	// lag is the reference index minus the microphone index of the echo.
	delay := centre + int64(lag)
	// The reference handed out for microphone sample m must hold the audio
	// the microphone hears at m+Lead: newer by Lead than the echo's own.
	want := delay + Lead
	switch {
	case !a.locked:
		a.offset, a.locked, a.havePend = want, true, false
		a.Locks++
	case abs64(want-a.offset) <= 16: // within 1 ms: peak-picking noise
		a.havePend = false
	case a.havePend && abs64(want-a.pending) <= 16:
		a.offset, a.havePend = want, false
		a.Moves++
	default:
		a.pending, a.havePend = want, true
	}
}

// xcorrPeak finds the lag in [-max, max] at which mic best matches ref, where
// ref[max+lag : max+lag+len(mic)] is compared with mic. Coarse at Rate/decim,
// then refined at full rate around the coarse peak. Returns the lag and the
// normalised correlation there.
func xcorrPeak(mic, ref []int16, max int) (int, float64) {
	cm, cr := downsample(mic), downsample(ref)
	cmax := max / decim
	best, bestV := 0, -1.0
	for l := -cmax; l <= cmax; l++ {
		v := math.Abs(dot(cm, cr[cmax+l:]))
		if v > bestV {
			best, bestV = l, v
		}
	}
	best *= decim
	fine, fineV := best, -1.0
	for l := best - decim; l <= best+decim; l++ {
		if l < -max || l > max {
			continue
		}
		v := math.Abs(dot(mic, ref[max+l:]))
		if v > fineV {
			fine, fineV = l, v
		}
	}
	seg := ref[max+fine : max+fine+len(mic)]
	n := norm(mic) * norm(seg)
	if n == 0 {
		return fine, 0
	}
	return fine, fineV / n
}

// downsample averages blocks of decim samples: a crude low-pass, enough for
// speech-band correlation.
func downsample(x []int16) []float64 {
	out := make([]float64, len(x)/decim)
	for i := range out {
		var s float64
		for _, v := range x[i*decim : (i+1)*decim] {
			s += float64(v)
		}
		out[i] = s
	}
	return out
}

type num interface{ ~int16 | ~float64 }

func dot[T num](a, b []T) float64 {
	var s float64
	for i := range a {
		s += float64(a[i]) * float64(b[i])
	}
	return s
}

func norm(x []int16) float64 { return math.Sqrt(dot(x, x)) }

func rms(x []int16) float64 {
	if len(x) == 0 {
		return 0
	}
	return math.Sqrt(dot(x, x) / float64(len(x)))
}

func abs64(x int64) int64 {
	if x < 0 {
		return -x
	}
	return x
}

// decimator is 48 kHz stereo S16 to 16 kHz mono (left), through a windowed
// sinc low-pass at 7 kHz so nothing above the microphones' band folds in.
type decimator struct {
	taps  []float64
	hist  []float64 // twice decTaps: each sample written twice, so a window is always contiguous
	pos   int
	phase int
}

const decTaps = 63

func newDecimator() decimator {
	t := make([]float64, decTaps)
	fc := 7000.0 / 48000.0
	var sum float64
	for i := range t {
		m := float64(i - decTaps/2)
		s := 2 * fc
		if m != 0 {
			s = math.Sin(2*math.Pi*fc*m) / (math.Pi * m)
		}
		w := 0.54 - 0.46*math.Cos(2*math.Pi*float64(i)/float64(decTaps-1))
		t[i] = s * w
		sum += t[i]
	}
	for i := range t {
		t[i] /= sum
	}
	return decimator{taps: t, hist: make([]float64, 2*decTaps)}
}

func (d *decimator) run(raw []byte) []int16 {
	n := len(raw) / 4
	out := make([]int16, 0, n/3+1)
	for i := 0; i < n; i++ {
		v := float64(int16(binary.LittleEndian.Uint16(raw[4*i:])))
		d.hist[d.pos], d.hist[d.pos+decTaps] = v, v
		d.pos = (d.pos + 1) % decTaps
		d.phase++
		if d.phase < 3 {
			continue
		}
		d.phase = 0
		var s float64
		h := d.hist[d.pos : d.pos+decTaps] // oldest first
		for k, c := range d.taps {
			s += c * h[k]
		}
		if s > 32767 {
			s = 32767
		} else if s < -32768 {
			s = -32768
		}
		out = append(out, int16(math.Round(s)))
	}
	return out
}

// Offset is the current reference-minus-microphone offset, for logs and
// tools; meaningful only while Locked.
func (a *Aligner) Offset() int64 {
	a.smu.Lock()
	defer a.smu.Unlock()
	return a.offset
}
