// ble_probe — run the production BLE scanner at a chosen scan interval and
// window and report what Bermuda would get from it, so the cost of a scan
// cadence to WiFi (measured AP-side, as retries per frame to this device) can
// be set against the coverage it buys.
//
// The chip shares one antenna between WiFi and Bluetooth (WMT_SOC.cfg
// coex_wmt_ant_mode=1), and at the shipped 320/30ms the AP retried >100% of
// frames to a proxying Dot against 0.2% with the proxy off (2026-09-23).
//
// Coverage is judged against the requirement in internal/bluetooth/emit.go:
// roughly one advertisement per device per second. For every address seen in
// at least half the run, it reports the share of 1s bins holding one.
//
// The server must NOT be scanning (bleProxyEnabled off for this device):
// /dev/stpbt is single-owner. Build with device/tools/build_tools.sh, then:
//
//	ble_probe -interval 320 -window 30 -seconds 240
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bluetooth"
)

func main() {
	interval := flag.Int("interval", 320, "LE scan interval, ms")
	window := flag.Int("window", 30, "LE scan window, ms")
	seconds := flag.Int("seconds", 240, "run length")
	raw := flag.Bool("raw", false, "bench session (-tags bench) instead of the production scanner")
	scan := flag.Bool("scan", true, "raw mode: enable the LE scan (false = Bluetooth up, idle)")
	vendor := flag.String("vendor", "", "raw mode: vendor commands after reset, e.g. fc7a:03401f401f0004,fc79:078000060707")
	burstOn := flag.Int("burst-on", 0, "raw mode: scan on for this many ms per burst (with -burst-off)")
	burstOff := flag.Int("burst-off", 0, "raw mode: scan off for this many ms between bursts")
	trace := flag.String("trace", "", "raw mode: write 'ms addr rssi' per advert to this file")
	flag.Parse()

	if *raw {
		runRaw(*interval, *window, *seconds, *scan, *vendor, *burstOn, *burstOff, *trace)
		return
	}

	// The scanner reads its cadence from the environment at session start.
	os.Setenv("BLE_SCAN_INTERVAL_MS", strconv.Itoa(*interval))
	os.Setenv("BLE_SCAN_WINDOW_MS", strconv.Itoa(*window))

	var mu sync.Mutex
	start := time.Now()
	bins := map[string]map[int]bool{} // addr -> set of 1s bins with an advert
	reports := 0

	sc := bluetooth.NewScanner(func(batch []bluetooth.Advert) {
		b := int(time.Since(start) / time.Second)
		mu.Lock()
		defer mu.Unlock()
		for _, a := range batch {
			reports++
			if bins[a.Addr] == nil {
				bins[a.Addr] = map[int]bool{}
			}
			bins[a.Addr][b] = true
		}
	})
	startUnix := time.Now().Unix()
	sc.SetEnabled(true)
	time.Sleep(time.Duration(*seconds) * time.Second)
	sc.SetEnabled(false)
	stats := sc.Stats()

	mu.Lock()
	defer mu.Unlock()
	n := *seconds
	var cover []float64
	for _, s := range bins {
		if len(s) >= n/2 {
			cover = append(cover, float64(len(s))/float64(n))
		}
	}
	sort.Float64s(cover)
	pct := func(p float64) float64 {
		if len(cover) == 0 {
			return 0
		}
		return cover[int(p*float64(len(cover)-1))]
	}
	out, _ := json.Marshal(map[string]any{
		"start":       startUnix,
		"end":         time.Now().Unix(),
		"intervalMs":  *interval,
		"windowMs":    *window,
		"duty":        float64(*window) / float64(*interval),
		"addrs":       len(bins),
		"steadyAddrs": len(cover),
		"coverP10":    pct(0.10),
		"coverP50":    pct(0.50),
		"coverP90":    pct(0.90),
		"batchedAdv":  reports,
		"advertsSeen": stats.AdvertsSeen,
		"hciErrors":   stats.HciErrors,
		"restarts":    stats.Restarts,
	})
	fmt.Println(string(out))
}
