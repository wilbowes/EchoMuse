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
