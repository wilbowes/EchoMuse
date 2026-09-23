//go:build bench

package bluetooth

// Bench-only HCI session for tools/ble_probe: the chip brought up exactly as
// the scanner does, with two variables the scanner does not expose — vendor
// commands sent after reset, and whether to scan at all. It answers what the
// WiFi cost of the proxy is made of (2026-09-23): the scan parameters turned
// out not to change it, so the questions are whether Bluetooth being ON costs
// WiFi without a scan, and whether Amazon's vendor init (which we skip)
// changes that.
//
// No watchdog: a session that is deliberately not scanning is silent.

import (
	"fmt"
	"os"
	"time"
)

// VendorCmd is one HCI command sent after reset, before any scan.
type VendorCmd struct {
	Opcode uint16
	Params []byte
}

// BenchOptions describe one bench session.
type BenchOptions struct {
	Vendor     []VendorCmd
	Scan       bool
	IntervalMs int
	WindowMs   int
	Duration   time.Duration
	// BurstOn/BurstOff, when both set, toggle the scan from here: the chip
	// ignores the interval and window (measured 2026-09-23), but obeys enable.
	BurstOn   time.Duration
	BurstOff  time.Duration
	OnAdverts func([]Advert)
	Logf       func(format string, args ...any)
}

// BenchSession opens /dev/stpbt, resets, sends the vendor commands, optionally
// scans, and holds the controller up for Duration. The caller must ensure
// nothing else owns /dev/stpbt.
func BenchSession(o BenchOptions) error {
	f, err := os.OpenFile(devPath, os.O_RDWR, 0)
	if err != nil {
		return fmt.Errorf("open %s: %w", devPath, err)
	}
	defer f.Close()

	events := make(chan []byte, 256)
	go func() {
		var parser h4Parser
		buf := make([]byte, 2048)
		for {
			n, err := f.Read(buf)
			if err != nil {
				close(events)
				return
			}
			for _, pkt := range parser.Feed(buf[:n]) {
				select {
				case events <- pkt:
				default:
				}
			}
		}
	}()

	send := func(opcode uint16, params []byte) error {
		if _, err := f.Write(buildCommand(opcode, params)); err != nil {
			return fmt.Errorf("write cmd %04x: %w", opcode, err)
		}
		deadline := time.After(cmdTimeout)
		for {
			select {
			case pkt, ok := <-events:
				if !ok {
					return fmt.Errorf("read closed during cmd %04x", opcode)
				}
				if cc, ok := parseCommandComplete(pkt); ok && cc.opcode == opcode {
					o.Logf("cmd %04x status 0x%02x ret % x", opcode, cc.status, cc.params)
					if cc.status != 0 {
						return fmt.Errorf("cmd %04x status 0x%02x", opcode, cc.status)
					}
					return nil
				}
				// A vendor command may answer with Command Status (0x0f)
				// rather than Command Complete; log anything else seen.
				if len(pkt) >= 2 && pkt[0] == h4TypeEvent && pkt[1] != 0x3e {
					o.Logf("event during cmd %04x: % x", opcode, pkt)
				}
			case <-deadline:
				return fmt.Errorf("cmd %04x timeout", opcode)
			}
		}
	}

	if err := send(opReset, nil); err != nil {
		return err
	}
	for _, v := range o.Vendor {
		if err := send(v.Opcode, v.Params); err != nil {
			return err
		}
	}
	if o.Scan {
		if err := send(opLESetScanParams, scanParams(o.IntervalMs, o.WindowMs)); err != nil {
			return err
		}
		if err := send(opLESetScanEnable, []byte{0x01, 0x00}); err != nil {
			return err
		}
	}

	// Toggling fires from a timer, but the enable command must not be sent
	// from there: send() reads the same events channel as the loop below.
	// So the loop sends it, and adverts arriving meanwhile are taken by
	// send() and lost — a few per toggle, which the counts can bear.
	var toggle <-chan time.Time
	scanning := o.Scan
	burst := o.Scan && o.BurstOn > 0 && o.BurstOff > 0
	if burst {
		toggle = time.After(o.BurstOn)
	}

	end := time.After(o.Duration)
	for {
		select {
		case pkt, ok := <-events:
			if !ok {
				return fmt.Errorf("read closed")
			}
			if adverts := parseAdvReports(pkt); len(adverts) > 0 && o.OnAdverts != nil {
				o.OnAdverts(adverts)
			}
		case <-toggle:
			scanning = !scanning
			en := byte(0x00)
			next := o.BurstOff
			if scanning {
				en, next = 0x01, o.BurstOn
			}
			if err := send(opLESetScanEnable, []byte{en, 0x00}); err != nil {
				return err
			}
			toggle = time.After(next)
		case <-end:
			if scanning {
				_ = send(opLESetScanEnable, []byte{0x00, 0x00})
			}
			return nil
		}
	}
}
