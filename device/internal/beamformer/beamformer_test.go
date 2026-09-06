package beamformer

import "testing"

// warmBeamformer returns a Beamformer with baseline warmed up and a uniform
// noise floor, as if it had been running in a quiet room.
func warmBeamformer(baseline float64) *Beamformer {
	b := New()
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

	if b.lockedChannel != directionToChannel[2] {
		t.Fatalf("lock-back picked ch%d, want ch%d (direction 2 burst)",
			b.lockedChannel, directionToChannel[2])
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

	if b.lockedChannel != directionToChannel[4] {
		t.Fatalf("fallback picked ch%d, want ch%d (live onset direction 4)",
			b.lockedChannel, directionToChannel[4])
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
	b := New()
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
	b := New()
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
	b := New()
	if out := b.EchoRef(make([]byte, frameSize-1)); out != nil {
		t.Fatal("a short period must report no reference, not a partial one")
	}
}
