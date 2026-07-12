package bluetooth

import (
	"fmt"
	"log"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	devPath = "/dev/stpbt"

	cmdTimeout   = 3 * time.Second
	retryBackoff = 5 * time.Second

	// Batch flush: whichever comes first. Adverts are coalesced per
	// (address+payload) within the window (see ingest), so flushCount is a
	// count of *distinct* adverts, not raw reports — a dense room collapses
	// to a bounded handful of JSON messages per second on the control WS.
	// This bound matters: the MT8163 shares one weak core with the audio
	// pipeline (which already GC-stalls the mic every ~25s), so an
	// unbounded advert rate starved the control-WS goroutine into keepalive
	// ping timeouts (fleet disconnects, 2026-07-12).
	flushInterval = 250 * time.Millisecond
	flushCount    = 48

	// Watchdog: with duplicate filtering off there is always BLE chatter in
	// range; a silent chip means the WMT power-save machinery (mtk_stp_psm)
	// or a firmware wedge stopped the scan. Re-init is cheap.
	watchdogQuiet = 30 * time.Second

	// Unique-address window for the stats gauge.
	uniqueWindow = 5 * time.Minute
)

// Stats is the diagnostics snapshot folded into the periodic device stats
// message (JSON tags are the wire shape the controller/dashboard read).
type Stats struct {
	Scanning    bool   `json:"scanning"`
	AdvertsSeen uint64 `json:"advertsSeen"`
	AdvertsSent uint64 `json:"advertsSent"`
	UniqueAddrs int    `json:"uniqueAddrs"`
	HciErrors   uint64 `json:"hciErrors"`
	Restarts    uint64 `json:"restarts"`
	BdAddr      string `json:"bdAddr,omitempty"`
}

// BatchCallback receives batched advertisements for upstream delivery.
// Called from the scanner's flush goroutine; must not block for long.
type BatchCallback func([]Advert)

// Scanner owns the /dev/stpbt transport and the passive-scan lifecycle.
// SetEnabled(true/false) follows the controller's bleProxyEnabled config;
// the run loop keeps the scan alive (with backoff + watchdog re-init)
// until disabled.
type Scanner struct {
	onBatch BatchCallback

	mu      sync.Mutex
	enabled bool
	stopCh  chan struct{} // closes to stop the current run loop
	doneCh  chan struct{} // closed by the run loop on exit

	// batch buffer — coalesced per (address+payload) so repeat broadcasts of
	// identical data collapse to one advert carrying the latest RSSI.
	batchMu sync.Mutex
	pending map[string]Advert

	// stats
	advertsSeen atomic.Uint64
	advertsSent atomic.Uint64
	hciErrors   atomic.Uint64
	restarts    atomic.Uint64
	scanning    atomic.Bool
	bdAddrMu    sync.Mutex
	bdAddr      string
	uniqueMu    sync.Mutex
	unique      map[string]time.Time

	bluedroidDisabled bool
}

func NewScanner(onBatch BatchCallback) *Scanner {
	return &Scanner{
		onBatch: onBatch,
		unique:  make(map[string]time.Time),
		pending: make(map[string]Advert),
	}
}

// batchKey coalesces identical broadcasts: same address AND same payload
// collapse to one entry (latest RSSI wins). Distinct payloads from the same
// address (e.g. ADV_IND vs SCAN_RSP) keep separate entries, so no advertised
// data is lost — only exact repeats are thinned.
func batchKey(a Advert) string {
	return a.Addr + "\x00" + string(a.Data)
}

// SetEnabled starts or stops the scanner. Idempotent; safe from the config
// callback. Stopping is synchronous (scan disabled, device closed) and mu is
// held across the whole transition — including the wait for the run loop to
// exit — so an enable arriving mid-disable can never spawn a second owner of
// /dev/stpbt. (The run loop never takes mu, so holding it here can't deadlock.)
func (s *Scanner) SetEnabled(enabled bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if enabled == s.enabled {
		return
	}
	s.enabled = enabled
	if enabled {
		s.stopCh = make(chan struct{})
		s.doneCh = make(chan struct{})
		go s.run(s.stopCh, s.doneCh)
		log.Println("[ble] scanner enabled")
		return
	}
	stopCh, doneCh := s.stopCh, s.doneCh
	s.stopCh, s.doneCh = nil, nil
	close(stopCh)
	<-doneCh
	log.Println("[ble] scanner disabled")
}

// Stats returns the current diagnostics snapshot.
func (s *Scanner) Stats() Stats {
	s.uniqueMu.Lock()
	cutoff := time.Now().Add(-uniqueWindow)
	for k, t := range s.unique {
		if t.Before(cutoff) {
			delete(s.unique, k)
		}
	}
	uniqueCount := len(s.unique)
	s.uniqueMu.Unlock()
	s.bdAddrMu.Lock()
	bdAddr := s.bdAddr
	s.bdAddrMu.Unlock()
	return Stats{
		Scanning:    s.scanning.Load(),
		AdvertsSeen: s.advertsSeen.Load(),
		AdvertsSent: s.advertsSent.Load(),
		UniqueAddrs: uniqueCount,
		HciErrors:   s.hciErrors.Load(),
		Restarts:    s.restarts.Load(),
		BdAddr:      bdAddr,
	}
}

// run keeps a scan session alive until stopCh closes, backing off between
// failed attempts. Each session: open → reset → params → enable → read.
func (s *Scanner) run(stopCh, doneCh chan struct{}) {
	defer close(doneCh)
	first := true
	for {
		select {
		case <-stopCh:
			return
		default:
		}
		if !first {
			s.restarts.Add(1)
			select {
			case <-stopCh:
				return
			case <-time.After(retryBackoff):
			}
		}
		first = false
		if err := s.session(stopCh); err != nil {
			s.hciErrors.Add(1)
			log.Printf("[ble] scan session ended: %v", err)
		}
	}
}

// session runs one full scan lifecycle; returns when stopCh closes (nil)
// or on transport/watchdog error.
func (s *Scanner) session(stopCh chan struct{}) error {
	s.ensureBluedroidDisabled()

	// Opening triggers WMT BT function-on + firmware patch download.
	f, err := os.OpenFile(devPath, os.O_RDWR, 0)
	if err != nil {
		return fmt.Errorf("open %s: %w", devPath, err)
	}
	defer f.Close()

	// events carries every parsed HCI event out of the read pump; activity
	// feeds the watchdog. The pump exits when the fd closes.
	events := make(chan []byte, 64)
	readErr := make(chan error, 1)
	go func() {
		var parser h4Parser
		buf := make([]byte, 2048)
		for {
			n, err := f.Read(buf)
			if err != nil {
				readErr <- err
				return
			}
			for _, pkt := range parser.Feed(buf[:n]) {
				select {
				case events <- pkt:
				default: // never block the pump; drop under backlog
				}
			}
		}
	}()

	sendCmd := func(opcode uint16, params []byte) (commandComplete, error) {
		if _, err := f.Write(buildCommand(opcode, params)); err != nil {
			return commandComplete{}, fmt.Errorf("write cmd %04x: %w", opcode, err)
		}
		deadline := time.After(cmdTimeout)
		for {
			select {
			case pkt := <-events:
				if cc, ok := parseCommandComplete(pkt); ok && cc.opcode == opcode {
					if cc.status != 0 {
						return cc, fmt.Errorf("cmd %04x status 0x%02x", opcode, cc.status)
					}
					return cc, nil
				}
			case err := <-readErr:
				return commandComplete{}, fmt.Errorf("read during cmd %04x: %w", opcode, err)
			case <-deadline:
				return commandComplete{}, fmt.Errorf("cmd %04x timeout", opcode)
			case <-stopCh:
				return commandComplete{}, fmt.Errorf("stopped")
			}
		}
	}

	if _, err := sendCmd(opReset, nil); err != nil {
		return err
	}
	if cc, err := sendCmd(opReadBdAddr, nil); err == nil {
		s.bdAddrMu.Lock()
		s.bdAddr = formatBdAddr(cc.params)
		s.bdAddrMu.Unlock()
	}
	// 320/30 = ~9% radio duty, the standard passive-scan cadence (matches
	// ESPHome's esp32_ble_tracker default). The original 100/50 (50% duty)
	// caught ~7x the advert volume and starved this SoC's audio+control
	// goroutines into WS ping-timeout disconnects (2026-07-12).
	intervalMs := envIntDefault("BLE_SCAN_INTERVAL_MS", 320)
	windowMs := envIntDefault("BLE_SCAN_WINDOW_MS", 30)
	if _, err := sendCmd(opLESetScanParams, scanParams(intervalMs, windowMs)); err != nil {
		return err
	}
	// filter_duplicates=0 — every advert is forwarded so the controller/HA
	// (Bermuda) sees continuous RSSI updates.
	if _, err := sendCmd(opLESetScanEnable, []byte{0x01, 0x00}); err != nil {
		return err
	}
	s.scanning.Store(true)
	defer s.scanning.Store(false)
	log.Printf("[ble] passive scan running (interval=%dms window=%dms bdaddr=%s)",
		intervalMs, windowMs, s.bdAddr)

	flushTicker := time.NewTicker(flushInterval)
	defer flushTicker.Stop()
	defer s.flush()
	watchdog := time.NewTimer(watchdogQuiet)
	defer watchdog.Stop()

	for {
		select {
		case pkt := <-events:
			if !watchdog.Stop() {
				<-watchdog.C
			}
			watchdog.Reset(watchdogQuiet)
			if adverts := parseAdvReports(pkt); len(adverts) > 0 {
				s.ingest(adverts)
			}
		case <-flushTicker.C:
			s.flush()
		case <-watchdog.C:
			return fmt.Errorf("watchdog: no HCI events for %s — re-initialising", watchdogQuiet)
		case err := <-readErr:
			return fmt.Errorf("read: %w", err)
		case <-stopCh:
			// Best-effort orderly shutdown: stop the scan so the chip idles.
			_, _ = f.Write(buildCommand(opLESetScanEnable, []byte{0x00, 0x00}))
			time.Sleep(100 * time.Millisecond)
			return nil
		}
	}
}

func (s *Scanner) ingest(adverts []Advert) {
	s.advertsSeen.Add(uint64(len(adverts)))
	now := time.Now()
	s.uniqueMu.Lock()
	for _, a := range adverts {
		s.unique[a.Addr] = now
	}
	s.uniqueMu.Unlock()
	s.batchMu.Lock()
	for _, a := range adverts {
		s.pending[batchKey(a)] = a
	}
	full := len(s.pending) >= flushCount
	s.batchMu.Unlock()
	if full {
		s.flush()
	}
}

func (s *Scanner) flush() {
	s.batchMu.Lock()
	if len(s.pending) == 0 {
		s.batchMu.Unlock()
		return
	}
	batch := make([]Advert, 0, len(s.pending))
	for _, a := range s.pending {
		batch = append(batch, a)
	}
	// Reuse the map — clear rather than reallocate (steady-state hot path).
	for k := range s.pending {
		delete(s.pending, k)
	}
	s.batchMu.Unlock()
	s.advertsSent.Add(uint64(len(batch)))
	if s.onBatch != nil {
		s.onBatch(batch)
	}
}

// ensureBluedroidDisabled durably disables the Android Bluetooth stack —
// /dev/stpbt is single-owner and Bluedroid holds it whenever enabled.
// `pm disable` persists across reboots and is idempotent; run once per
// process. Android's pm/settings are shebang-less wrappers, so exec via sh
// (same ENOEXEC constraint as svc in internal/wifi).
func (s *Scanner) ensureBluedroidDisabled() {
	if s.bluedroidDisabled {
		return
	}
	pkgs := []string{
		"com.android.bluetooth",
		"com.amazon.device.csmbluetooth.service",
		"com.amazon.device.csmbluetooth.headlessUxController",
		"com.amazon.device.bluetoothdfu",
	}
	for _, pkg := range pkgs {
		out, err := exec.Command("/system/bin/sh", "-c", "pm disable "+pkg).CombinedOutput()
		if err != nil {
			log.Printf("[ble] pm disable %s: %v — %s", pkg, err, strings.TrimSpace(string(out)))
		}
	}
	if out, err := exec.Command("/system/bin/sh", "-c",
		"settings put global bluetooth_on 0").CombinedOutput(); err != nil {
		log.Printf("[ble] settings bluetooth_on=0: %v — %s", err, strings.TrimSpace(string(out)))
	}
	s.bluedroidDisabled = true
	log.Println("[ble] Android Bluetooth stack disabled (persistent)")
}

func envIntDefault(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil && n > 0 {
			return n
		}
	}
	return def
}
