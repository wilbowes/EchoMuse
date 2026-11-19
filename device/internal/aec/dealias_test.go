package aec

import (
	"bytes"
	"encoding/binary"
	"math"
	"math/rand"
	"testing"
)

// output makes n frames of 48kHz stereo S16LE from f(i).
func output(n int, from int, f func(i int) float64) []byte {
	b := make([]byte, n*4)
	for i := 0; i < n; i++ {
		s := int16(f(from + i))
		binary.LittleEndian.PutUint16(b[i*4:], uint16(s))
		binary.LittleEndian.PutUint16(b[i*4+2:], uint16(s))
	}
	return b
}

// outputLR is output with a different signal on each side.
func outputLR(n int, from int, l, r func(i int) float64) []byte {
	b := make([]byte, n*4)
	for i := 0; i < n; i++ {
		binary.LittleEndian.PutUint16(b[i*4:], uint16(int16(l(from+i))))
		binary.LittleEndian.PutUint16(b[i*4+2:], uint16(int16(r(from+i))))
	}
	return b
}

// loopback is ch8 as the hardware makes it: every third output sample from
// output index p, no filter.
func loopback(n int, p int, f func(i int) float64) []byte {
	b := make([]byte, n*2)
	for i := 0; i < n; i++ {
		binary.LittleEndian.PutUint16(b[i*2:], uint16(int16(f(p+3*i))))
	}
	return b
}

// level is the amplitude of freq in a 16kHz S16LE buffer, by correlation.
func level(b []byte, freq float64) float64 {
	n := len(b) / 2
	var re, im float64
	for i := 0; i < n; i++ {
		x := float64(int16(binary.LittleEndian.Uint16(b[i*2:])))
		re += x * math.Cos(2*math.Pi*freq*float64(i)/16000)
		im += x * math.Sin(2*math.Pi*freq*float64(i)/16000)
	}
	return 2 * math.Hypot(re, im) / float64(n)
}

// twoTones is 1kHz and 12kHz, plus a little noise so that no stretch of
// it repeats: real audio does not, and a lock must be unique.
func twoTones(i int) float64 {
	t := float64(i) / 48000
	h := uint32(i) * 2654435761
	h ^= h >> 15
	noise := float64(int32(h%2001) - 1000)
	return 8000*math.Sin(2*math.Pi*1000*t) + 8000*math.Sin(2*math.Pi*12000*t) + noise
}

// A 12kHz tone in the output folds to 4kHz in ch8 at full level; the
// rebuilt reference keeps the 1kHz tone and drops the alias.
func TestDealiasRemovesTheAlias(t *testing.T) {
	d := newDealias()
	for k := 0; k < 12; k++ {
		d.feed(output(2048, k*2048, twoTones))
	}
	// Playback runs ~171ms behind the write; any phase of the three.
	ref := loopback(2560, 4*2048+2, twoTones)
	if a := level(ref, 4000); a < 7000 {
		t.Fatalf("test signal: ch8 alias at 4kHz is %.0f, want ~8000", a)
	}
	got := d.clean(ref)
	if bytes.Equal(got, ref) {
		t.Fatal("reference not rebuilt")
	}
	if a := level(got, 1000); math.Abs(a-8000) > 400 {
		t.Errorf("1kHz in the rebuilt reference: %.0f, want ~8000", a)
	}
	if a := level(got, 4000); a > 80 {
		t.Errorf("4kHz alias in the rebuilt reference: %.0f, want < 1%% of 8000", a)
	}
	if !d.locked || d.relocks != 0 || d.pos != 4*2048+2+3*2560 {
		t.Errorf("locked=%v relocks=%d at %d, want one lock at the true position", d.locked, d.relocks, d.pos-3*2560)
	}
}

// The next period follows on from the lock, and a jump in the output (a
// stream restart) is found again rather than cancelled against the wrong
// samples.
func TestDealiasFollowsAndRelocks(t *testing.T) {
	d := newDealias()
	for k := 0; k < 20; k++ {
		d.feed(output(2048, k*2048, twoTones))
	}
	p := 4*2048 + 1
	for k := 0; k < 3; k++ {
		ref := loopback(2560, p, twoTones)
		if got := d.clean(ref); bytes.Equal(got, ref) || level(got, 4000) > 80 {
			t.Fatalf("period %d not rebuilt", k)
		}
		p += 3 * 2560
	}
	p += 1000 // the output jumped
	ref := loopback(2560, p, twoTones)
	if got := d.clean(ref); bytes.Equal(got, ref) || level(got, 4000) > 80 {
		t.Fatal("not found again after a jump")
	}
	if d.relocks != 1 || d.losses != 1 {
		t.Errorf("relocks=%d losses=%d, want 1 and 1", d.relocks, d.losses)
	}
}

// With a plug in, Sendspin plays L != R (#273), and biscuit's ch8 loops back
// the RIGHT channel. Set to that side the ring matches it and the reference
// is rebuilt; left on the left, nothing matches and ch8 passes as captured,
// which is the fallback, not a wrong lock.
func TestDealiasLocksOnTheLoopbacksSide(t *testing.T) {
	left := func(i int) float64 { return 0.6 * twoTones(i+5000) }
	for _, side := range []int{1, 0} {
		d := newDealias()
		d.setSide(side)
		for k := 0; k < 12; k++ {
			d.feed(outputLR(2048, k*2048, left, twoTones))
		}
		ref := loopback(2560, 4*2048+2, twoTones) // the right channel
		got := d.clean(ref)
		if side == 1 {
			if !d.locked || bytes.Equal(got, ref) || level(got, 4000) > 80 {
				t.Errorf("side 1: locked=%v, alias %.0f: not rebuilt from the right channel", d.locked, level(got, 4000))
			}
			if d.pos != 4*2048+2+3*2560 {
				t.Errorf("side 1: locked at %d, want %d", d.pos-3*2560, 4*2048+2)
			}
		} else if d.locked || !bytes.Equal(got, ref) {
			t.Errorf("side 0: locked=%v on a loopback of the other channel", d.locked)
		}
	}
}

// On the mono audio the speaker plays without a plug both sides are the
// same samples, so the side changes nothing: the same lock, the same bytes.
func TestDealiasSideIsIrrelevantOnDualMono(t *testing.T) {
	var outs [2][][]byte
	var pos [2]int64
	for side := 0; side < 2; side++ {
		d := newDealias()
		d.setSide(side)
		for k := 0; k < 20; k++ {
			d.feed(output(2048, k*2048, twoTones))
		}
		p := 4*2048 + 1
		for k := 0; k < 3; k++ {
			outs[side] = append(outs[side], append([]byte(nil), d.clean(loopback(2560, p, twoTones))...))
			p += 3 * 2560
		}
		pos[side] = d.pos
		if !d.locked {
			t.Fatalf("side %d: not locked", side)
		}
	}
	if pos[0] != pos[1] {
		t.Errorf("locked at %d on the left, %d on the right", pos[0], pos[1])
	}
	for k := range outs[0] {
		if !bytes.Equal(outs[0][k], outs[1][k]) {
			t.Errorf("batch %d: the rebuilt reference depends on the side", k)
		}
	}
	// Out of range is the left, never an index past the frame.
	d := newDealias()
	d.setSide(7)
	if d.side.Load() != 0 {
		t.Errorf("side 7 stored as %d", d.side.Load())
	}
	var none *dealias
	none.setSide(1)

	// The Canceller passes the board's side through, and without a dealias
	// (a cleaned reference) has nothing to set.
	c := New(1)
	c.SetRefSide(1)
	if c.dealias.side.Load() != 1 {
		t.Error("SetRefSide(1) did not reach the de-aliasing ring")
	}
	c.dealias = nil
	c.SetRefSide(1)
}

// A ch8 that is not the output, and silence, pass through unchanged.
func TestDealiasFallsBackToCh8(t *testing.T) {
	d := newDealias()
	for k := 0; k < 12; k++ {
		d.feed(output(2048, k*2048, twoTones))
	}
	rng := rand.New(rand.NewSource(1))
	other := loopback(2560, 0, func(int) float64 { return rng.NormFloat64() * 5000 })
	if got := d.clean(other); !bytes.Equal(got, other) {
		t.Error("a reference that is not the output was rewritten")
	}
	silent := make([]byte, 2560*2)
	if got := d.clean(silent); !bytes.Equal(got, silent) {
		t.Error("silence was rewritten")
	}
	var none *dealias
	if got := none.clean(other); !bytes.Equal(got, other) {
		t.Error("nil dealias rewrote the reference")
	}
}

// A loopback that never matches is searched three times, then left alone
// for dealiasBackoff batches, not searched on every one.
func TestDealiasBacksOffWhenNothingMatches(t *testing.T) {
	d := newDealias()
	for k := 0; k < 12; k++ {
		d.feed(output(2048, k*2048, twoTones))
	}
	rng := rand.New(rand.NewSource(2))
	other := loopback(2560, 0, func(int) float64 { return rng.NormFloat64() * 5000 })
	for i := 0; i < dealiasMaxMisses; i++ {
		d.clean(other)
	}
	if d.skip != dealiasBackoff || d.backoffs != 1 {
		t.Fatalf("skip=%d backoffs=%d after %d misses", d.skip, d.backoffs, dealiasMaxMisses)
	}
	// While backing off, even a reference that would match is passed as is.
	ref := loopback(2560, 4*2048, twoTones)
	if got := d.clean(ref); !bytes.Equal(got, ref) || d.skip != dealiasBackoff-1 {
		t.Fatalf("searched during the back-off (skip=%d)", d.skip)
	}
}

// Following a lock allocates nothing: it runs on the mic goroutine, which
// has a deadline.
func TestDealiasFollowingDoesNotAllocate(t *testing.T) {
	d := newDealias()
	for k := 0; k < 30; k++ {
		d.feed(output(2048, k*2048, twoTones))
	}
	p := 2048
	d.clean(loopback(2560, p, twoTones))
	refs := make([][]byte, 5)
	for i := range refs {
		p += 3 * 2560
		refs[i] = loopback(2560, p, twoTones)
	}
	i := 0
	allocs := testing.AllocsPerRun(len(refs)-1, func() {
		d.clean(refs[i])
		i++
	})
	if allocs != 0 || !d.locked {
		t.Errorf("%.0f allocations per period (locked=%v)", allocs, d.locked)
	}
}

// End to end through the Canceller, as the device runs it: WriteFar is fed
// every period (and must feed the ring even on the hardware path, where it
// returns early), the mic hears a filtered echo, and ch8 is the aliased
// copy. White noise puts two thirds of its energy above 8kHz.
func TestCancellerUsesTheDealiasedReference(t *testing.T) {
	att := func(dealiased bool) float64 {
		c := hwCanceller(64)
		if !dealiased {
			c.dealias = nil
		}
		rng := rand.New(rand.NewSource(3))
		const periods = 140 // 6s
		out := make([]float64, periods*2048)
		for i := range out {
			out[i] = rng.NormFloat64() * 4000
		}
		f := func(i int) float64 {
			if i < 0 || i >= len(out) {
				return 0
			}
			return out[i]
		}
		echo := newDealias() // its filter, as the codec's for the mics
		const ahead = 4      // periods written before they play
		for k := 0; k < ahead; k++ {
			c.WriteFar(output(2048, k*2048, f))
		}
		var in, res float64
		played := 0
		for k := ahead; k < periods; k++ {
			c.WriteFar(output(2048, k*2048, f))
			for played+3*2560+3*dealiasTaps < (k-ahead+1)*2048 {
				ref := loopback(2560, played, f)
				mic := make([]byte, 2560*2)
				for i := 0; i < 2560; i++ {
					acc := 0.0
					for j, h := range echo.fir {
						acc += h * f(played+3*(i-20)-dealiasHalf+j)
					}
					binary.LittleEndian.PutUint16(mic[i*2:], uint16(int16(-0.5*acc)))
				}
				got := c.ProcessWithRef(mic, ref)
				if played > 3*16000*3 { // after 3s
					in += rms(mic)
					res += rms(got)
				}
				played += 3 * 2560
			}
		}
		return 20 * math.Log10(in/res)
	}
	with, without := att(true), att(false)
	t.Logf("cancellation: %.1fdB de-aliased, %.1fdB on ch8 as captured", with, without)
	if with < 20 || without > 10 {
		t.Errorf("de-aliased %.1fdB (want > 20), as captured %.1fdB (want < 10)", with, without)
	}
}
