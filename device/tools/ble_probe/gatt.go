//go:build bench

package main

import (
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

// runList prints every advertiser heard, strongest first.
func runList(seconds int) {
	seen, err := bluetooth.ScanList(time.Duration(seconds)*time.Second, log.Printf)
	if err != nil {
		log.Fatal(err)
	}
	sort.Slice(seen, func(i, j int) bool { return seen[i].Rssi > seen[j].Rssi })
	for _, s := range seen {
		out, _ := json.Marshal(s)
		fmt.Println(string(out))
	}
}

// runGatt drives bluetooth.GattProbe and prints one JSON result, with each
// connection's read round trips reduced to a median and a maximum.
func runGatt(peers string, intervalMs, hold, readEveryMs int, scan, discover, notify bool, write string) {
	var targets []bluetooth.GattTarget
	for _, item := range strings.Split(peers, ",") {
		addr, typ, _ := strings.Cut(item, "/")
		n, err := strconv.Atoi(typ)
		if typ != "" && (err != nil || n < 0 || n > 1) {
			log.Fatalf("bad address type in %q", item)
		}
		targets = append(targets, bluetooth.GattTarget{Addr: addr, AddrType: n})
	}
	start := time.Now().Unix()
	res, err := bluetooth.GattProbe(bluetooth.GattOptions{
		Targets:        targets,
		ConnIntervalMs: intervalMs,
		Hold:           time.Duration(hold) * time.Second,
		ReadEvery:      time.Duration(readEveryMs) * time.Millisecond,
		Scan:           scan,
		Discover:       discover,
		Notify:         notify,
		Write:          write,
		Logf:           log.Printf,
	})
	type rtt struct {
		P50 float64 `json:"p50"`
		Max float64 `json:"max"`
	}
	rtts := make([]rtt, len(res.Conns))
	for i := range res.Conns {
		r := res.Conns[i].ReadRttMs
		res.Conns[i].ReadRttMs = nil
		if len(r) > 0 {
			sort.Float64s(r)
			rtts[i] = rtt{r[len(r)/2], r[len(r)-1]}
		}
	}
	out, _ := json.Marshal(map[string]any{
		"start": start, "end": time.Now().Unix(), "askedIntervalMs": intervalMs,
		"readEveryMs": readEveryMs, "holdScan": scan, "result": res,
		"readRttMs": rtts, "err": fmt.Sprint(err),
	})
	fmt.Println(string(out))
	if err != nil {
		os.Exit(1)
	}
}
