package aec

import (
	"encoding/binary"
	"log"
	"math"
	"sync"
	"sync/atomic"
	"time"
)

// dealias rebuilds the hardware reference without its aliases.
//
// ch8 is the playback looped back into the 16kHz mic capture by keeping
// every third sample of the 48kHz output, with NO anti-aliasing filter:
// tones at 9, 12, 15 and 20kHz arrive in ch8 at 7, 4, 1 and 4kHz at full
// level, while the microphones (filtered by the codec) show nothing there
// (VVV, 2026-10-18). Whatever the output carries above 8kHz is therefore
// in-band in the reference and absent from the echo, and the canceller
// chases it: pink noise through the firmware measured 1dB of cancellation,
// the same capture with the reference rebuilt from the output 25.7dB.
// Music always has content up there, so this is what held barge-in back.
// The capture refuses 48kHz, so it cannot be fixed at the source.
//
// The cure is the output itself. WriteFar sees every period written to
// ALSA; ch8 is an exact copy of every third sample of it (to 1 LSB), so
// matching the two gives the reference's position in the output to the
// sample, with none of the drift the software tap had to be steered for.
// The reference handed to the canceller is then the same samples
// decimated through a proper low-pass. When no match is found (the tap
// is not being fed, or a board's loopback is filtered or scaled) ch8 is
// used unchanged, as before, so a board needs no setting for this.
type dealias struct {
	// mu guards the ring and head, which the speaker's write loop fills.
	// Nothing slow happens under it: a search runs on a copy (#866).
	mu   sync.Mutex
	ring [dealiasRing]int16 // the output's side channel (below), 48kHz
	head int64              // samples ever written; ring index is head % dealiasRing

	// side is the output channel the loopback carries, 0 left or 1 right
	// (board.MicArray.EchoRefSide), the one the ring keeps. Equal on the
	// mono audio the speaker plays without a plug; with a plug in, Sendspin
	// plays L != R (#273) and only the loopback's own side matches.
	side atomic.Int32

	// The rest belongs to the mic goroutine (clean).
	fir        [dealiasTaps]float64
	locked     bool
	pos        int64 // output index of the next ch8 sample, when locked
	everLocked bool
	misses     int // consecutive searches that found nothing
	skip       int // batches to pass before searching again
	snap       []int16
	span       []int16
	out        []byte
	// Since the last report: relocks (a capture overrun or a stream
	// restart moves the reference), losses, and back-offs.
	relocks, losses, backoffs int
	reported                  time.Time
}

const (
	dealiasRing = 1 << 16 // 1.37s: the ALSA write-ahead is 171ms
	dealiasTaps = 95      // odd, so the filter's centre is a whole sample
	dealiasHalf = dealiasTaps / 2
	// dealiasTol: ch8 is the S16 output shifted into 24 bits; extracted
	// back at unity it differs by at most 1.
	dealiasTol = 2
	// A fresh lock needs this many non-silent samples to agree, so a quiet
	// stretch cannot match by chance. Following one needs every sample to
	// agree and no count, so a quiet passage does not drop it.
	dealiasMinMatch = 64
	// After this many searches in a row find nothing in loud audio, the
	// next dealiasBackoff batches (160ms each) skip the search: a board
	// whose loopback never matches would otherwise search on every one.
	dealiasMaxMisses = 3
	dealiasBackoff   = 30
	dealiasReport    = time.Minute
)

func newDealias() *dealias {
	d := &dealias{snap: make([]int16, dealiasRing)}
	// Windowed sinc (Blackman), cut at 7.2kHz: 0.15 of 48kHz.
	const fc = 7200.0 / 48000.0
	sum := 0.0
	for i := range d.fir {
		n := float64(i - dealiasHalf)
		h := 2 * fc
		if n != 0 {
			h = math.Sin(2*math.Pi*fc*n) / (math.Pi * n)
		}
		w := 0.42 - 0.5*math.Cos(2*math.Pi*float64(i)/(dealiasTaps-1)) +
			0.08*math.Cos(4*math.Pi*float64(i)/(dealiasTaps-1))
		d.fir[i] = h * w
		sum += d.fir[i]
	}
	for i := range d.fir {
		d.fir[i] /= sum
	}
	return d
}

// feed appends one output period (48kHz stereo S16LE). Speaker goroutine.
func (d *dealias) feed(period []byte) {
	if d == nil {
		return
	}
	n := len(period) / 4
	off := 2 * int(d.side.Load())
	d.mu.Lock()
	for i := 0; i < n; i++ {
		d.ring[(d.head+int64(i))%dealiasRing] = int16(binary.LittleEndian.Uint16(period[i*4+off:]))
	}
	d.head += int64(n)
	d.mu.Unlock()
}

// setSide picks the output channel the ring keeps: 1 is the right, anything
// else the left, so a bad value can never index past the frame on the
// speaker goroutine. Safe on nil.
func (d *dealias) setSide(side int) {
	if d == nil {
		return
	}
	if side != 1 {
		side = 0
	}
	d.side.Store(int32(side))
}

// window is the ring as of one moment: the live one under mu, or a copy.
type window struct {
	ring []int16
	head int64
}

func (w window) at(i int64) int16 { return w.ring[i%dealiasRing] }

// fits reports whether n ch8 samples from output index p, and the
// filter's reach either side of them, are all still in the ring.
func (w window) fits(p int64, n int) bool {
	return p-dealiasHalf >= max(0, w.head-dealiasRing) && p+3*int64(n-1)+dealiasHalf < w.head
}

// matches reports whether ref (16kHz S16LE) is every third output sample
// from p, with at least need non-silent samples agreeing.
func (w window) matches(ref []byte, p int64, need int) bool {
	n := len(ref) / 2
	if !w.fits(p, n) {
		return false
	}
	agree := 0
	for i := 0; i < n; i++ {
		r := int16(binary.LittleEndian.Uint16(ref[i*2:]))
		if diff := int(r) - int(w.at(p+3*int64(i))); diff > dealiasTol || diff < -dealiasTol {
			return false
		}
		if r > 64 || r < -64 {
			agree++
		}
	}
	return agree >= need
}

// search finds the output index ref starts at, newest first. loud is
// false when ref has nothing to match on, which is not a miss.
func (w window) search(ref []byte) (p int64, ok, loud bool) {
	n := len(ref) / 2
	i0 := -1
	for i := 0; i < n; i++ {
		if r := int16(binary.LittleEndian.Uint16(ref[i*2:])); r > 64 || r < -64 {
			i0 = i
			break
		}
	}
	if i0 < 0 {
		return 0, false, false
	}
	r0 := int(int16(binary.LittleEndian.Uint16(ref[i0*2:])))
	for j := w.head - 1; j >= max(0, w.head-dealiasRing); j-- {
		if diff := int(w.at(j)) - r0; diff > dealiasTol || diff < -dealiasTol {
			continue
		}
		if p := j - 3*int64(i0); w.matches(ref, p, dealiasMinMatch) {
			return p, true, true
		}
	}
	return 0, false, true
}

// copySpan fills d.span with the output samples the filter needs for n
// ch8 samples from p.
func (d *dealias) copySpan(w window, p int64, n int) {
	size := 3*(n-1) + dealiasTaps
	if cap(d.span) < size {
		d.span = make([]int16, size)
	}
	d.span = d.span[:size]
	for i := range d.span {
		d.span[i] = w.at(p - dealiasHalf + int64(i))
	}
}

// clean returns ref rebuilt from the output, or ref itself when it cannot
// be placed. The returned slice is valid until the next call. Mic
// goroutine.
func (d *dealias) clean(ref []byte) []byte {
	if d == nil {
		return ref
	}
	n := len(ref) / 2
	defer d.report()
	silent := true
	for i := 0; i < n; i++ {
		if binary.LittleEndian.Uint16(ref[i*2:]) != 0 {
			silent = false
			break
		}
	}
	if silent {
		// Nothing playing: an all-zero reference is already clean, and a
		// held lock carries on through it.
		if d.locked {
			d.pos += 3 * int64(n)
		}
		return ref
	}

	if d.locked {
		d.mu.Lock()
		w := window{d.ring[:], d.head}
		if w.matches(ref, d.pos, 0) {
			d.copySpan(w, d.pos, n)
			d.mu.Unlock()
			d.pos += 3 * int64(n)
			return d.filter(n)
		}
		d.mu.Unlock()
		d.locked = false
		d.losses++
	}

	if d.skip > 0 {
		d.skip--
		return ref
	}
	// Searched on a copy, so the speaker never waits on it.
	d.mu.Lock()
	copy(d.snap, d.ring[:])
	w := window{d.snap, d.head}
	d.mu.Unlock()
	p, ok, loud := w.search(ref)
	if !ok {
		if loud {
			if d.misses++; d.misses >= dealiasMaxMisses {
				d.misses, d.skip = 0, dealiasBackoff
				d.backoffs++
			}
		}
		return ref
	}
	d.misses = 0
	if d.everLocked {
		d.relocks++
	} else {
		d.everLocked = true
		log.Printf("[aec] de-aliased reference locked to the output")
	}
	d.locked = true
	d.copySpan(w, p, n)
	d.pos = p + 3*int64(n)
	return d.filter(n)
}

// filter decimates d.span through the low-pass into d.out.
func (d *dealias) filter(n int) []byte {
	if cap(d.out) < n*2 {
		d.out = make([]byte, n*2)
	}
	d.out = d.out[:n*2]
	for i := 0; i < n; i++ {
		acc := 0.0
		for k, h := range d.fir {
			acc += h * float64(d.span[3*i+k])
		}
		binary.LittleEndian.PutUint16(d.out[i*2:], uint16(int16(max(-32768, min(32767, math.Round(acc))))))
	}
	return d.out
}

// report logs what happened since the last report, at most once a
// dealiasReport: a board that matches only some of the time would
// otherwise log on every batch.
func (d *dealias) report() {
	now := time.Now()
	if now.Sub(d.reported) < dealiasReport {
		return
	}
	if d.relocks+d.losses+d.backoffs > 0 {
		log.Printf("[aec] de-aliased reference: locked=%v relocks=%d losses=%d backoffs=%d in %.0fs",
			d.locked, d.relocks, d.losses, d.backoffs, now.Sub(d.reported).Seconds())
	}
	d.relocks, d.losses, d.backoffs, d.reported = 0, 0, 0, now
}
