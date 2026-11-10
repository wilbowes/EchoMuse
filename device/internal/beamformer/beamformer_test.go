package beamformer

import (
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/board"
)

// The tests run on biscuit's array, so they assert what they always did.
var (
	biscuit     = board.BiscuitMics()
	nChannels   = biscuit.Channels
	byteSample  = 3
	frameSize   = nChannels * byteSample
	centreCh    = biscuit.Centre
	echoRefCh   = biscuit.EchoRef
	NumMics     = biscuit.NumMics()
	nDirections = len(biscuit.Perimeter)
)

// warmBeamformer returns a Beamformer with baseline warmed up and a uniform
// noise floor, as if it had been running in a quiet room.
func warmBeamformer(baseline float64) *Beamformer {
	b := New(biscuit)
	b.baselineReady = 100
	for di := 0; di < nDirections; di++ {
		b.energyBaseline[di] = baseline
	}
	return b
}

// TestLockBackPicksPastBurst is the scenario that motivated lock-back:
// the wake word was spoken ~1s ago from direction 2, the fast smoother has
// since decayed and (thanks to a TV) now points at direction 5. Live onset
// selection picks the TV; lock-back must pick the speaker.
func TestLockBackPicksPastBurst(t *testing.T) {
	b := warmBeamformer(1e-6)

	// Fill the ring with baseline-level noise…
	for i := 0; i < historyPeriods; i++ {
		for di := 0; di < nDirections; di++ {
			b.energyHistory[i][di] = 1e-6
		}
	}
	b.historyCount = historyPeriods

	// …with a wake-word burst on direction 2, ~10 periods long, in the
	// middle of the window (well before "now").
	for i := 20; i < 30; i++ {
		b.energyHistory[i][2] = 5e-4
	}

	// TV on direction 5: elevated steady energy in both the ring and the
	// live smoother — loud in absolute terms, but not a burst relative to
	// its own baseline.
	b.energyBaseline[5] = 4e-4
	for i := 0; i < historyPeriods; i++ {
		b.energyHistory[i][5] = 5e-4
	}
	b.energySmooth[5] = 5e-4 // live smoother points at the TV
	b.energySmooth[2] = 2e-6 // speaker's onset has decayed

	b.Lock(true)

	if b.lockedChannel != b.directionToChannel[2] {
		t.Fatalf("lock-back picked ch%d, want ch%d (direction 2 burst)",
			b.lockedChannel, b.directionToChannel[2])
	}
}

// TestLockFallsBackToOnsetRatioWithoutHistory — fresh start: baseline warm
// (carried into the ready state quickly) but ring not yet populated. Must
// use the live onset ratio, not a zero-filled ring.
func TestLockFallsBackToOnsetRatioWithoutHistory(t *testing.T) {
	b := warmBeamformer(1e-6)
	b.historyCount = 0
	b.energySmooth[4] = 3e-4 // live onset on direction 4

	b.Lock(true)

	if b.lockedChannel != b.directionToChannel[4] {
		t.Fatalf("fallback picked ch%d, want ch%d (live onset direction 4)",
			b.lockedChannel, b.directionToChannel[4])
	}
}

// TestLockDisabledIsNoOp — beamforming off must leave the channel unlocked
// (ch6 omni output path).
func TestLockDisabledIsNoOp(t *testing.T) {
	b := warmBeamformer(1e-6)
	b.historyCount = historyPeriods
	b.energyHistory[0][3] = 1.0

	b.Lock(false)

	if b.lockedChannel != -1 {
		t.Fatalf("Lock(false) locked to ch%d, want unlocked (-1)", b.lockedChannel)
	}
}

// TestBurstRatioTopNMean checks the allocation-free partial selection:
// history 1..64 on direction 0 → top 8 are 57..64, mean 60.5.
func TestBurstRatioTopNMean(t *testing.T) {
	b := warmBeamformer(1.0)
	for i := 0; i < historyPeriods; i++ {
		b.energyHistory[i][0] = float64(i + 1)
	}
	b.historyCount = historyPeriods

	got := b.burstRatio(0)
	want := 60.5 // mean of 57..64, baseline 1.0
	if got != want {
		t.Fatalf("burstRatio = %v, want %v", got, want)
	}
}

// TestBurstRatioPartialHistory — fewer samples than burstTopN averages what
// exists instead of diluting with zeros.
func TestBurstRatioPartialHistory(t *testing.T) {
	b := warmBeamformer(1.0)
	b.energyHistory[0][0] = 4.0
	b.energyHistory[1][0] = 2.0
	b.historyCount = 2

	got := b.burstRatio(0)
	want := 3.0
	if got != want {
		t.Fatalf("burstRatio = %v, want %v", got, want)
	}
}

// ─── Hardware echo reference (#385) ───────────────────────────────────────────

// raw9 builds one period of 9-channel S24_3LE with a per-channel constant, so
// each channel is identifiable by value alone.
func raw9(frames int, valueFor func(ch int) int32) []byte {
	buf := make([]byte, frames*frameSize)
	for f := 0; f < frames; f++ {
		for ch := 0; ch < nChannels; ch++ {
			v := valueFor(ch)
			b := f*frameSize + ch*byteSample
			buf[b] = byte(v)
			buf[b+1] = byte(v >> 8)
			buf[b+2] = byte(v >> 16)
		}
	}
	return buf
}

func TestEchoRefReadsChannel8(t *testing.T) {
	b := New(biscuit)
	// Every channel gets a distinct value; ch8 gets one we can recognise.
	raw := raw9(periodFrames, func(ch int) int32 {
		if ch == echoRefCh {
			return 0x200000 // +2097152 of 2^23 → 8192 after the 24→16 shift
		}
		return int32(ch) << 12
	})
	out := b.EchoRef(raw)
	if len(out) != periodFrames*2 {
		t.Fatalf("expected %d bytes, got %d", periodFrames*2, len(out))
	}
	got := int16(uint16(out[0]) | uint16(out[1])<<8)
	if got != 8192 {
		t.Fatalf("EchoRef read the wrong channel or gain: got %d, want 8192", got)
	}
}

// TestEchoRefIsUnityGain is the one that matters. Mic extraction applies
// micGainDb (+24dB by default) pre-truncation because speech sits at about
// -70dBFS. The reference is the playback stream at full digital scale — it
// measured -7.3dBFS on hardware — and the same gain on that is 17dB of hard
// clipping, which does not merely cancel badly: it teaches the adaptive
// filter a distorted echo path.
func TestEchoRefIsUnityGain(t *testing.T) {
	b := New(biscuit)
	// Near full scale on ch8. Any gain above unity clamps this.
	raw := raw9(periodFrames, func(ch int) int32 {
		if ch == echoRefCh {
			return 0x7F0000 >> 0 // 8323072 — close to the 2^23 ceiling
		}
		return 0
	})
	out := b.EchoRef(raw)
	got := int16(uint16(out[0]) | uint16(out[1])<<8)
	if got == 32767 || got == -32768 {
		t.Fatalf("reference clipped at %d — EchoRef must extract at unity gain", got)
	}
	if b.ClippedSamples() != 0 {
		t.Fatalf("reference extraction clipped %d samples", b.ClippedSamples())
	}
}

func TestEchoRefRejectsShortBuffer(t *testing.T) {
	b := New(biscuit)
	if out := b.EchoRef(make([]byte, frameSize-1)); out != nil {
		t.Fatal("a short period must report no reference, not a partial one")
	}
}

func TestWakeMicSelectsUnlockedChannel(t *testing.T) {
	b := New(biscuit)
	for _, c := range []struct{ mic, want int }{{0, centreCh}, {1, 0}, {3, 2}, {6, 5}, {7, centreCh}, {-1, centreCh}} {
		b.SetWakeMic(c.mic)
		if got := b.omniChannel(); got != c.want {
			t.Errorf("wakeMic %d: channel %d, want %d", c.mic, got, c.want)
		}
	}
}

// MicChannels hands the canceller every microphone, in channel order, at the
// mic gain, and the entry for the microphone in use IS what Process returned
// (#814). OutputChannel must name that microphone in every state, or the
// canceller reports and returns the wrong one.
func TestMicChannelsCarriesEveryMicrophone(t *testing.T) {
	raw := raw9(periodFrames, func(ch int) int32 { return int32(ch+1) << 16 })
	sample := func(b []byte) int16 { return int16(uint16(b[0]) | uint16(b[1])<<8) }

	for _, tc := range []struct {
		name    string
		prepare func(*Beamformer)
		steer   float64
		want    int
	}{
		{"unlocked, centre", func(*Beamformer) {}, -1, centreCh},
		{"unlocked, wake microphone 3", func(b *Beamformer) { b.SetWakeMic(3) }, -1, 2},
		{"locked", func(b *Beamformer) { b.lockedChannel = 4 }, -1, 4},
		{"locked, fixed beam at 90 degrees", func(b *Beamformer) { b.lockedChannel = 4 }, 90, 2},
		{"unlocked ignores the fixed beam", func(*Beamformer) {}, 90, centreCh},
	} {
		b := New(biscuit)
		tc.prepare(b)
		mono, _ := b.Process(raw, tc.steer, 2.0)
		sel := b.OutputChannel(tc.steer)
		if sel != tc.want {
			t.Errorf("%s: OutputChannel = ch%d, want ch%d", tc.name, sel, tc.want)
			continue
		}
		mics := b.MicChannels(raw, 2.0, sel, mono)
		if len(mics) != NumMics {
			t.Fatalf("%s: %d microphones, want %d", tc.name, len(mics), NumMics)
		}
		if &mics[sel][0] != &mono[0] {
			t.Errorf("%s: the microphone in use is not the buffer Process returned", tc.name)
		}
		for ch, m := range mics {
			if len(m) != periodFrames*2 {
				t.Fatalf("%s: ch%d is %d bytes, want %d", tc.name, ch, len(m), periodFrames*2)
			}
			// (ch+1)<<16 at gain 2 is (ch+1)*512 after the 24-to-16 shift.
			if got, want := sample(m), int16((ch+1)*512); got != want {
				t.Errorf("%s: ch%d reads %d, want %d", tc.name, ch, got, want)
			}
		}
	}
}

// ClippedSamples describes the audio in use: six more microphones extracted
// for the canceller must not multiply it.
func TestMicChannelsDoesNotCountClipping(t *testing.T) {
	b := New(biscuit)
	raw := raw9(periodFrames, func(int) int32 { return 0x7fffff })
	mono, _ := b.Process(raw, -1, 16.0)
	want := b.ClippedSamples()
	if want != periodFrames {
		t.Fatalf("the microphone in use clipped %d samples, want %d", want, periodFrames)
	}
	b.MicChannels(raw, 16.0, centreCh, mono)
	if got := b.ClippedSamples(); got != want {
		t.Fatalf("extracting the other microphones moved the clipped count from %d to %d", want, got)
	}
}

// A board with no centre microphone listens on its first edge one, and every
// direction reads its own channel however the channels are ordered round the
// edge: the Dot 3's run ch1, ch2, ch3, ch0 clockwise.
func TestArrayWithoutACentreMicrophone(t *testing.T) {
	a := board.MicArray{Channels: 4, Centre: -1, EchoRef: -1,
		Perimeter: []board.Mic{{Ch: 1, Angle: 45}, {Ch: 2, Angle: 135}, {Ch: 3, Angle: 225}, {Ch: 0, Angle: 315}}}
	b := New(a)
	if b.NumMics() != 4 {
		t.Fatalf("%d microphones, want 4", b.NumMics())
	}
	for _, c := range []struct{ mic, want int }{{0, 1}, {1, 1}, {2, 2}, {4, 0}, {5, 1}, {-1, 1}} {
		b.SetWakeMic(c.mic)
		if got := b.omniChannel(); got != c.want {
			t.Errorf("wakeMic %d: channel %d, want %d", c.mic, got, c.want)
		}
	}
	raw := make([]byte, periodFrames*4*byteSample)
	for f := 0; f < periodFrames; f++ {
		for ch := 0; ch < 4; ch++ {
			v := int32(ch+1) << 16
			o := (f*4 + ch) * byteSample
			raw[o], raw[o+1], raw[o+2] = byte(v), byte(v>>8), byte(v>>16)
		}
	}
	b.decodeChannels(raw)
	for d, m := range a.Perimeter {
		if got, want := b.chanBuf[d][0], float32(m.Ch+1)/128; got != want {
			t.Errorf("direction %d read %v, want ch%d's %v", d, got, m.Ch, want)
		}
	}
	if b.EchoRef(raw) != nil {
		t.Error("a frame with no loopback must report no reference")
	}
	if got := b.OutputChannel(-1); got != 1 {
		t.Errorf("unlocked output ch%d, want ch1", got)
	}
}

// S32_LE carries the same 24 bits in the top three bytes of four, so a value
// reads the same as S24_3LE does, the low byte (always 0 on the Dot 3) is
// ignored, and extraction and decoding agree.
func TestS32SamplesReadAsTheirTop24Bits(t *testing.T) {
	a := board.MicArray{Channels: 4, Format: board.S32LE, Centre: -1, EchoRef: -1,
		Perimeter: []board.Mic{{Ch: 1, Angle: 45}, {Ch: 2, Angle: 135}, {Ch: 3, Angle: 225}, {Ch: 0, Angle: 315}}}
	b := New(a)
	raw := make([]byte, periodFrames*4*4)
	for f := 0; f < periodFrames; f++ {
		for ch := 0; ch < 4; ch++ {
			v := uint32(int32(-(ch+1))<<16)<<8 | 0x5a // 24 bits of -(ch+1)<<16, junk below
			o := (f*4 + ch) * 4
			raw[o], raw[o+1], raw[o+2], raw[o+3] = byte(v), byte(v>>8), byte(v>>16), byte(v>>24)
		}
	}
	b.decodeChannels(raw)
	for d, m := range a.Perimeter {
		if got, want := b.chanBuf[d][0], -float32(m.Ch+1)/128; got != want {
			t.Errorf("direction %d decoded %v, want %v", d, got, want)
		}
	}
	mono, _ := b.Process(raw, -1, 1.0)
	if len(mono) != periodFrames*2 {
		t.Fatalf("%d bytes, want %d", len(mono), periodFrames*2)
	}
	// ch1 (the wake mic, no centre) is -(2<<16) of 2^23: -512 after 24-to-16.
	if got := int16(uint16(mono[0]) | uint16(mono[1])<<8); got != -512 {
		t.Errorf("ch1 extracted %d, want -512", got)
	}
}
