//go:build bench

package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bluetooth"
)

// runRaw drives bluetooth.BenchSession: the chip up with no scanner around it,
// optionally with Amazon's vendor init, optionally not scanning.
func runRaw(interval, window, seconds int, scan bool, vendor string, burstOn, burstOff int, trace string) {
	var cmds []bluetooth.VendorCmd
	for _, item := range strings.Split(vendor, ",") {
		if item == "" {
			continue
		}
		op, params, ok := strings.Cut(item, ":")
		n, err := strconv.ParseUint(op, 16, 16)
		if !ok || err != nil {
			log.Fatalf("bad vendor command %q", item)
		}
		p, err := hex.DecodeString(params)
		if err != nil {
			log.Fatalf("bad vendor params %q: %v", item, err)
		}
		cmds = append(cmds, bluetooth.VendorCmd{Opcode: uint16(n), Params: p})
	}

	var tf *os.File
	if trace != "" {
		var err error
		if tf, err = os.Create(trace); err != nil {
			log.Fatal(err)
		}
		defer tf.Close()
	}
	t0 := time.Now()
	start := t0.Unix()
	adverts := 0
	// Per-address set of Bermuda cycles (1.05s) that saw an advert.
	cycles := map[string]map[int]bool{}
	err := bluetooth.BenchSession(bluetooth.BenchOptions{
		Vendor:     cmds,
		Scan:       scan,
		IntervalMs: interval,
		WindowMs:   window,
		Duration:   time.Duration(seconds) * time.Second,
		BurstOn:    time.Duration(burstOn) * time.Millisecond,
		BurstOff:   time.Duration(burstOff) * time.Millisecond,
		OnAdverts: func(a []bluetooth.Advert) {
			ms := time.Since(t0).Milliseconds()
			for _, x := range a {
				adverts++
				c := int(ms / 1050)
				if cycles[x.Addr] == nil {
					cycles[x.Addr] = map[int]bool{}
				}
				cycles[x.Addr][c] = true
				if tf != nil {
					fmt.Fprintf(tf, "%d %s %d\n", ms, x.Addr, x.Rssi)
				}
			}
		},
		Logf: log.Printf,
	})
	// Coverage over devices present for most of the run: share of Bermuda
	// cycles holding at least one advert.
	total := int(time.Since(t0).Milliseconds() / 1050)
	var cover []float64
	for _, c := range cycles {
		if len(c) >= total/3 {
			cover = append(cover, float64(len(c))/float64(total))
		}
	}
	sort.Float64s(cover)
	out, _ := json.Marshal(map[string]any{
		"burstOnMs": burstOn, "burstOffMs": burstOff, "addrs": len(cycles),
		"steadyAddrs": len(cover), "cycleCover": cover,
		"start": start, "end": time.Now().Unix(), "raw": true, "scan": scan,
		"vendor": vendor, "intervalMs": interval, "windowMs": window,
		"advertsSeen": adverts, "err": fmt.Sprint(err),
	})
	fmt.Println(string(out))
	if err != nil {
		os.Exit(1)
	}
}
