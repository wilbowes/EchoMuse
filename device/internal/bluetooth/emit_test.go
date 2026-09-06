package bluetooth

import (
	"fmt"
	"testing"
	"time"
)

func adv(addr string, rssi int, data ...byte) Advert {
	if len(data) == 0 {
		data = []byte{0x02, 0x01, 0x06}
	}
	return Advert{Addr: addr, Rssi: rssi, Data: data}
}

func TestFirstSightingIsForwardedButNotUrgent(t *testing.T) {
	g := newEmitGate()
	now := time.Now()

	keep, urgent := g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60)}, now)

	if len(keep) != 1 {
		t.Fatalf("a device never seen before must be forwarded, got %d", len(keep))
	}
	if urgent {
		t.Fatal("a first sighting must NOT cut the tick short — privacy addresses " +
			"rotate, so an unseen address is as likely to be a rename as an arrival")
	}
}

// The defect this test exists for: BLE privacy addresses rotate roughly every
// 15 minutes, so treating an unseen address as an arrival makes the urgent
// path fire continuously in any room with phones in it — emitting more small
// writes than the plain batching it replaced, on the goroutine that reads HCI.
func TestRotatingPrivacyAddressesNeverCutTheTickShort(t *testing.T) {
	g := newEmitGate()
	start := time.Now()

	urgentCount := 0
	for i := 0; i < 200; i++ {
		// A phone re-randomising its address on every broadcast: the
		// pathological case, and the shape of the real one.
		a := adv(fmt.Sprintf("7A:BB:CC:DD:%02X:%02X", i/256, i%256), -60)
		if _, urgent := g.admit([]Advert{a}, start.Add(time.Duration(i)*100*time.Millisecond)); urgent {
			urgentCount++
		}
	}
	if urgentCount != 0 {
		t.Fatalf("rotating addresses triggered %d urgent flushes; must be 0", urgentCount)
	}
}

func TestARepeatedIdenticalBroadcastIsDropped(t *testing.T) {
	g := newEmitGate()
	now := time.Now()
	g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60)}, now)

	// A beacon repeating itself 100ms later at the same signal level.
	keep, urgent := g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60)}, now.Add(100*time.Millisecond))

	if len(keep) != 0 {
		t.Fatalf("an unchanged repeat inside the floor must be dropped, got %d", len(keep))
	}
	if urgent {
		t.Fatal("a dropped advert must not force a flush")
	}
}

func TestAChangedPayloadIsForwardedImmediately(t *testing.T) {
	g := newEmitGate()
	now := time.Now()
	g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60, 0x02, 0x01, 0x06)}, now)

	// Same address, different payload 10ms later — a button press or a
	// sensor reading. A KNOWN address changing what it says is the one case
	// latency genuinely matters for, and the only thing that cuts the tick.
	keep, urgent := g.admit(
		[]Advert{adv("AA:BB:CC:DD:EE:01", -60, 0x02, 0x01, 0x1A)},
		now.Add(10*time.Millisecond),
	)

	if len(keep) != 1 {
		t.Fatal("a changed payload must be forwarded, whatever the floor")
	}
	if !urgent {
		t.Fatal("a known address changing payload must cut the batch tick short")
	}
}

func TestRssiMustMoveByTheThresholdAndClearTheFloor(t *testing.T) {
	g := newEmitGate()
	now := time.Now()
	g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60)}, now)

	// Inside HA's own smoothing, past the floor: not worth the control plane.
	if keep, _ := g.admit(
		[]Advert{adv("AA:BB:CC:DD:EE:01", -62)},
		now.Add(300*time.Millisecond),
	); len(keep) != 0 {
		t.Fatalf("a %ddB move is below the %ddB threshold and must be dropped",
			2, emitRssiDeltaDb)
	}

	// A real move, but inside the floor: rate-limited, or a fluctuating
	// signal restores the firehose on its own.
	if keep, _ := g.admit(
		[]Advert{adv("AA:BB:CC:DD:EE:01", -70)},
		now.Add(50*time.Millisecond),
	); len(keep) != 0 {
		t.Fatal("an RSSI move inside emitMinInterval must be rate-limited")
	}

	// A real move, past the floor.
	if keep, _ := g.admit(
		[]Advert{adv("AA:BB:CC:DD:EE:01", -70)},
		now.Add(300*time.Millisecond),
	); len(keep) != 1 {
		t.Fatalf("a %ddB move past the floor must be forwarded", 10)
	}
}

func TestASilentBeaconIsRefreshedOncePerSecond(t *testing.T) {
	g := newEmitGate()
	now := time.Now()
	g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60)}, now)

	// Bermuda re-decides area every second, so a perfectly stationary
	// beacon still has to be refreshed at that cadence.
	if keep, _ := g.admit(
		[]Advert{adv("AA:BB:CC:DD:EE:01", -60)},
		now.Add(emitMaxSilence),
	); len(keep) != 1 {
		t.Fatal("a stationary beacon must still be refreshed at emitMaxSilence")
	}
}

func TestTheScannerNeverGoesSilentPastTheGlobalCeiling(t *testing.T) {
	g := newEmitGate()
	now := time.Now()
	g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60)}, now)

	// Contrived but the case that matters: a device whose payload and
	// signal never change, and nothing else in range. HA retires a scanner
	// that says nothing for SCANNER_WATCHDOG_TIMEOUT (90s), so something
	// has to go out well before then.
	late := now.Add(emitGlobalMaxSilence)
	keep, _ := g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60)}, late)
	if len(keep) == 0 {
		t.Fatal("the gate must forward something rather than let HA retire the scanner")
	}
	if emitGlobalMaxSilence >= 90*time.Second {
		t.Fatalf("emitGlobalMaxSilence %s leaves no margin on HA's 90s scanner watchdog",
			emitGlobalMaxSilence)
	}
}

func TestAlternatingPayloadsKeepSeparateFloors(t *testing.T) {
	g := newEmitGate()
	now := time.Now()
	a := adv("AA:BB:CC:DD:EE:01", -60, 0x02, 0x01, 0x06)
	b := adv("AA:BB:CC:DD:EE:01", -60, 0x03, 0x02, 0x0A)

	g.admit([]Advert{a}, now)
	g.admit([]Advert{b}, now.Add(10*time.Millisecond))

	// Both payloads are now known. Alternating between them must not read
	// as "changed" every time, or a device that rotates service data
	// bypasses the gate completely.
	total := 0
	for i := 0; i < 10; i++ {
		at := now.Add(time.Duration(20+i*20) * time.Millisecond)
		next := a
		if i%2 == 1 {
			next = b
		}
		keep, _ := g.admit([]Advert{next}, at)
		total += len(keep)
	}
	if total != 0 {
		t.Fatalf("alternating known payloads inside the floor must be dropped, forwarded %d", total)
	}
}

func TestTheTableTracksDevicesInRangeNotEveryDeviceEverSeen(t *testing.T) {
	g := newEmitGate()
	now := time.Now()

	g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60)}, now)
	if g.tracked() != 1 {
		t.Fatalf("expected 1 tracked entry, got %d", g.tracked())
	}

	// A device that has left. Pruning is what stops the table growing for
	// the life of the process in a place with passing phones.
	g.prune(now.Add(emitEntryTTL + time.Second))
	if g.tracked() != 0 {
		t.Fatalf("a device gone for longer than the TTL must be dropped, %d left", g.tracked())
	}
}

func TestASteadilyBroadcastingDeviceIsNotPrunedWhileBeingFiltered(t *testing.T) {
	g := newEmitGate()
	now := time.Now()
	g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60)}, now)

	// Seen constantly, forwarded rarely. If `seen` only advanced on
	// emission the sweep would retire it, and it would then re-enter as a
	// "new device" — an arrival event for something that never left.
	at := now
	for i := 0; i < 60; i++ {
		at = at.Add(10 * time.Second)
		g.admit([]Advert{adv("AA:BB:CC:DD:EE:01", -60)}, at)
		g.prune(at)
	}
	if g.tracked() != 1 {
		t.Fatal("a device broadcasting throughout must stay tracked, not re-arrive")
	}
}

// The property this exists for: output rate is bounded by the number of
// devices in range, NOT by how fast they broadcast.
//
// That is what makes the saving scale — a beacon at 100ms costs the same as
// one at 500ms, so the busier the room the more the gate removes. Asserting
// a fixed reduction ratio instead would be asserting a property of the test's
// chosen beacon interval.
func TestOutputScalesWithDevicesNotWithBroadcastRate(t *testing.T) {
	const (
		devices  = 20
		duration = 60 * time.Second
	)

	run := func(everyMs int) (seen, sent int) {
		g := newEmitGate()
		start := time.Now()
		for t0 := time.Duration(0); t0 < duration; t0 += time.Duration(everyMs) * time.Millisecond {
			batch := make([]Advert, 0, devices)
			for d := 0; d < devices; d++ {
				// Stationary, RSSI dithering by 1dB — inside the threshold
				// and inside HA's own smoothing, which is the point.
				rssi := -60 - (int(t0/time.Second)+d)%2
				batch = append(batch, adv(fmt.Sprintf("AA:BB:CC:DD:EE:%02X", d), rssi))
			}
			seen += len(batch)
			keep, _ := g.admit(batch, start.Add(t0))
			sent += len(keep)
		}
		return seen, sent
	}

	// One per device per second is the requirement Bermuda's per-second area
	// decision sets. Allow a little slack for the first-sighting emission.
	expected := devices * int(duration/emitMaxSilence)

	for _, everyMs := range []int{100, 200, 500} {
		seen, sent := run(everyMs)
		if sent < expected || sent > expected+devices {
			t.Errorf("at %dms intervals: forwarded %d, expected ~%d (one per device per second)",
				everyMs, sent, expected)
		}
		t.Logf("%3dms intervals: forwarded %4d of %5d (%.1f%%, %.1fx reduction)",
			everyMs, sent, seen, 100*float64(sent)/float64(seen), float64(seen)/float64(sent))
	}
}
