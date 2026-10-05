package main

import (
	"encoding/hex"
	"fmt"
	"log"
	"strconv"
	"strings"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bluetooth"
)

// runHost drives the firmware's own path — the scanner's session with its
// connection manager — rather than the bench session: connect to each peer
// while the scan runs, discover, read what is readable, subscribe where a
// CCCD exists, optionally write, hold, disconnect.
func runHost(peers string, hold int, write string) {
	adverts := 0
	sc := bluetooth.NewScanner(func(b []bluetooth.Advert) { adverts += len(b) })
	m := sc.Conns()
	m.SetOwnAddress(bluetooth.StaticRandomAddr("ble_probe"))
	m.OnNotify = func(addr string, handle uint16, value []byte, ind bool) {
		log.Printf("notify %s 0x%04x indication=%v: % x %q", addr, handle, ind, value, value)
	}
	m.OnDisconnect = func(addr string, reason byte) {
		log.Printf("disconnected %s reason 0x%02x", addr, reason)
	}
	sc.SetEnabled(true)
	defer sc.SetEnabled(false)
	for deadline := time.Now().Add(20 * time.Second); ; time.Sleep(100 * time.Millisecond) {
		if free, _ := m.Slots(); free > 0 {
			break
		}
		if time.Now().After(deadline) {
			log.Fatal("the scan session never came up")
		}
	}

	wantUUID, wantHex, _ := strings.Cut(write, ":")
	var up []string
	for _, item := range strings.Split(peers, ",") {
		addr, typ, _ := strings.Cut(item, "/")
		n, _ := strconv.Atoi(typ)
		t0 := time.Now()
		info, err := m.Connect(addr, n)
		if err != nil {
			fmt.Printf("%s: connect: %v\n", addr, err)
			continue
		}
		up = append(up, addr)
		fmt.Printf("%s: connected in %dms, mtu %d, interval %.1fms\n", addr, time.Since(t0).Milliseconds(), info.MTU, info.IntervalMs)
		t0 = time.Now()
		services, err := m.Services(addr)
		if err != nil {
			fmt.Printf("%s: discovery: %v\n", addr, err)
			continue
		}
		fmt.Printf("%s: %d services in %dms\n", addr, len(services), time.Since(t0).Milliseconds())
		for _, sv := range services {
			fmt.Printf("service %s 0x%04x-0x%04x\n", sv.UUID, sv.Start, sv.End)
			for _, ch := range sv.Characteristics {
				line := fmt.Sprintf("  char %s props 0x%02x value 0x%04x", ch.UUID, ch.Properties, ch.ValueHandle)
				if ch.Properties&0x02 != 0 {
					if v, err := m.Read(addr, ch.ValueHandle); err != nil {
						line += " read: " + err.Error()
					} else {
						line += fmt.Sprintf(" = % x %q", v, v)
					}
				}
				fmt.Println(line)
				for _, d := range ch.Descriptors {
					if d.UUID.Is16(0x2902) && ch.Properties&0x30 != 0 {
						v := []byte{0x01, 0x00}
						if ch.Properties&0x10 == 0 {
							v = []byte{0x02, 0x00}
						}
						fmt.Printf("    subscribe 0x%04x: %v\n", d.Handle, m.Write(addr, d.Handle, v, true))
					}
				}
				if wantUUID != "" && strings.Contains(ch.UUID.String(), strings.ToLower(wantUUID)) {
					v, _ := hex.DecodeString(wantHex)
					fmt.Printf("  wrote % x: %v\n", v, m.Write(addr, ch.ValueHandle, v, true))
					back, err := m.Read(addr, ch.ValueHandle)
					fmt.Printf("  read back %q: %v\n", back, err)
				}
			}
		}
	}
	free, limit := m.Slots()
	fmt.Printf("slots %d/%d free; holding %ds with the scan running\n", free, limit, hold)
	before, seen := adverts, sc.Stats().AdvertsSeen
	time.Sleep(time.Duration(hold) * time.Second)
	st := sc.Stats()
	fmt.Printf("during hold: %d adverts seen, %d batched (restarts %d, hci errors %d)\n", st.AdvertsSeen-seen, adverts-before, st.Restarts, st.HciErrors)
	for _, addr := range up {
		fmt.Printf("%s: disconnect: %v\n", addr, m.Disconnect(addr))
	}
}
