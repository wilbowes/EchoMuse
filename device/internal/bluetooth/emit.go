package bluetooth

import (
	"sync"
	"time"
)

// Emission policy for scanned advertisements (#404).
//
// Passive scanning with duplicate filtering off produces one HCI event per
// broadcast, and a beacon typically broadcasts every 100-500ms. Forwarding
// all of it costs the device far more than anything downstream reads:
//
//   - Home Assistant's habluetooth tolerates 195s (connectable) to 900s
//     between advertisements per device before considering one stale, and
//     declares a SCANNER dead only after 90s of total silence.
//   - It smooths RSSI itself (EWMA alpha=0.3) and switches which proxy owns
//     a device on a 16dB difference with a 6dB deadband, so resolution finer
//     than a few dB changes no decision it makes.
//   - Bermuda re-decides which area a device is in every second.
//
// So the binding requirement is roughly one advertisement per device per
// second, and everything past that is waste — waste that lands on the
// control WebSocket, contending with the keepalive pong and the RTT echo.
//
// The gate is deliberately NOT the chip's duplicate filter
// (filter_duplicates=1), which suppresses identical advertisements: RSSI is
// the field that varies and the field Bermuda consumes, so the chip filter
// discards the signal and keeps the noise. Filtering here can be RSSI-aware
// in a way the chip cannot.
const (
	// A change worth forwarding. Below this is inside this hardware's RSSI
	// noise and inside HA's own smoothing.
	emitRssiDeltaDb = 3

	// Floor between RSSI-triggered emissions for one payload, so a device
	// whose signal is fluctuating cannot restore the firehose on its own.
	emitMinInterval = 250 * time.Millisecond

	// Ceiling on silence per payload — keeps Bermuda's per-second area
	// decision fed even for a beacon sitting perfectly still.
	emitMaxSilence = 1000 * time.Millisecond

	// Ceiling on silence for the scanner as a whole. HA's
	// SCANNER_WATCHDOG_TIMEOUT is 90s; this leaves 3x margin.
	emitGlobalMaxSilence = 30 * time.Second

	// Entries not seen for this long are dropped, so the table tracks the
	// devices in range rather than every device ever seen.
	emitEntryTTL = 5 * time.Minute
)

type emitEntry struct {
	rssi int
	sent time.Time
	seen time.Time
}

// emitGate decides which scanned advertisements are worth forwarding.
// Keyed per (address, payload) exactly as batchKey coalesces, so a device
// that alternates payloads — ADV_IND against a scan response, or a sensor
// rotating service data — keeps a separate floor for each rather than
// reading as a change on every broadcast.
//
// `addrs` tracks addresses independently of payload, and exists for one
// reason: **BLE privacy addresses rotate**, roughly every 15 minutes on
// phones and watches. A never-before-seen address is therefore NOT evidence
// that anything arrived — most of the time it is a device already in the
// room wearing a new name. Resolving that properly needs the IRK, which is
// Home Assistant's job and not something to attempt here.
type emitGate struct {
	mu      sync.Mutex
	entries map[string]emitEntry
	addrs   map[string]time.Time
	lastAny time.Time
	dropped uint64
}

func newEmitGate() *emitGate {
	return &emitGate{
		entries: make(map[string]emitEntry),
		addrs:   make(map[string]time.Time),
	}
}

// admit filters one batch of parsed advertisements.
//
// It returns the advertisements to forward and whether any of them is
// URGENT — a KNOWN address whose payload changed, which is the case latency
// actually matters for: a button press, a sensor reading, a beacon changing
// state. Those cut the batch tick short; everything else rides it.
//
// A first sighting of an address is deliberately NOT urgent, however new it
// looks. Privacy addresses rotate, so in a room with a few phones the
// "never seen this address" branch fires continuously — the first version of
// this treated each rotation as an arrival and flushed on it, which made the
// gate emit MORE small writes than the plain 250ms batching it replaced, on
// the same goroutine that reads HCI. Exactly backwards. A genuine arrival
// waits at most one tick, which nothing downstream can perceive: Home
// Assistant tolerates 195s.
func (g *emitGate) admit(adverts []Advert, now time.Time) (keep []Advert, urgent bool) {
	g.mu.Lock()
	defer g.mu.Unlock()

	// The scanner as a whole must not go quiet, or HA's watchdog retires it.
	// Checked before the loop so one advert can satisfy it.
	forceOne := !g.lastAny.IsZero() && now.Sub(g.lastAny) >= emitGlobalMaxSilence

	for _, a := range adverts {
		key := batchKey(a)
		prev, seen := g.entries[key]
		_, knownAddr := g.addrs[a.Addr]
		g.addrs[a.Addr] = now

		emit := false
		switch {
		case !seen:
			// A payload this address has not sent before. Urgent only if we
			// already knew the address — otherwise this is a first sighting,
			// which is as likely to be an address rotation as an arrival.
			emit = true
			if knownAddr {
				urgent = true
			}
		case now.Sub(prev.sent) >= emitMaxSilence:
			emit = true
		case abs(a.Rssi-prev.rssi) >= emitRssiDeltaDb &&
			now.Sub(prev.sent) >= emitMinInterval:
			emit = true
		case forceOne:
			emit = true
		}

		if !emit {
			// Still record the sighting, or the TTL sweep would retire a
			// device that is broadcasting steadily and being filtered.
			prev.seen = now
			g.entries[key] = prev
			g.dropped++
			continue
		}

		forceOne = false
		g.entries[key] = emitEntry{rssi: a.Rssi, sent: now, seen: now}
		g.lastAny = now
		keep = append(keep, a)
	}

	// Seed the global clock on the first batch, so a scanner that has never
	// forwarded anything does not read as 30s overdue.
	if g.lastAny.IsZero() {
		g.lastAny = now
	}
	return keep, urgent
}

// prune drops entries for devices that have left. Called from the flush
// ticker rather than per advertisement — this is the cold path.
func (g *emitGate) prune(now time.Time) {
	g.mu.Lock()
	defer g.mu.Unlock()
	for k, e := range g.entries {
		if now.Sub(e.seen) > emitEntryTTL {
			delete(g.entries, k)
		}
	}
	// Rotating addresses are the reason this map needs sweeping at all —
	// without it, a room with a few phones adds a permanent entry every
	// ~15 minutes for the life of the process.
	for a, seen := range g.addrs {
		if now.Sub(seen) > emitEntryTTL {
			delete(g.addrs, a)
		}
	}
}

// droppedCount reports advertisements filtered since start, for diagnostics.
// The useful figure is against Stats.AdvertsSeen — their ratio is the
// reduction this gate is achieving in the room it is actually in.
func (g *emitGate) droppedCount() uint64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.dropped
}

// tracked reports the current table size, for the same reason.
func (g *emitGate) tracked() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return len(g.entries)
}

func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}
