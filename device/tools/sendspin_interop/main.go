// sendspin_interop runs the device's Sendspin player against a real server,
// on a host, with a simulated DAC in place of the speaker. server.py is the
// other half: aiosendspin 9.1.1, the library Music Assistant ships. run.sh
// drives both.
//
// It exists because the spec and the shipped library disagree in several
// places, and the device has to work with the library. A unit test can only
// check this package against our reading of either.
//
// Each period the simulated DAC plays is logged with its first sample value
// and when it reaches the "DAC", on CLOCK_MONOTONIC_RAW — the clock the
// server stamps audio with — so server.py can measure the real sync error
// without trusting the client's own view of it.
package main

import (
	"bufio"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"github.com/wilbowes/EchoMuse/internal/sendspin"
)

func rawUs() int64 {
	var ts unix.Timespec
	unix.ClockGettime(unix.CLOCK_MONOTONIC_RAW, &ts)
	return ts.Sec*1_000_000 + ts.Nsec/1000
}

func emit(v map[string]any) {
	b, _ := json.Marshal(v)
	fmt.Println(string(b))
}

func main() {
	port := flag.Int("port", sendspin.DefaultPort, "listen port")
	store := flag.String("store", "/tmp/sendspin.json", "state file")
	unpaired := flag.Bool("unpaired", false, "admit unpaired servers")
	ppm := flag.Float64("dacppm", -560, "simulated DAC clock error, ppm")
	latency := flag.Duration("latency", 85*time.Millisecond, "simulated ALSA buffer")
	noise := flag.Duration("noise", time.Millisecond, "± error in measuring the DAC position")
	flag.Parse()

	cfg := sendspin.Config{
		StorePath: *store, Name: "Interop Echo", Instance: "interop-echo",
		Port: *port, Product: "interop", Version: "test", Unpaired: *unpaired,
	}
	c, err := sendspin.New(cfg)
	if err != nil {
		emit(map[string]any{"event": "error", "error": err.Error()})
		os.Exit(1)
	}
	c.OnSettings(func(v int, m bool, d int) {
		emit(map[string]any{"event": "settings", "volume": v, "muted": m, "delayMs": d})
	})
	if err := c.Start(); err != nil {
		emit(map[string]any{"event": "error", "error": err.Error()})
		os.Exit(1)
	}
	emit(map[string]any{"event": "ready", "clientId": c.ClientID(), "token": c.PairingToken()})
	var cur atomic.Pointer[sendspin.Client]
	cur.Store(c)

	// stdin carries commands from run.sh: "external on|off", "quit".
	quit := make(chan struct{})
	go func() {
		sc := bufio.NewScanner(os.Stdin)
		for sc.Scan() {
			switch sc.Text() {
			case "relink":
				// The controller link dropping and returning: the firmware
				// stops the player with goodbye "restart", then starts a new
				// one on the next config push. The server must redial.
				old := cur.Load()
				old.Stop("restart")
				time.Sleep(3 * time.Second)
				n, err := sendspin.New(cfg)
				if err == nil {
					err = n.Start()
				}
				if err != nil {
					emit(map[string]any{"event": "error", "error": err.Error()})
					continue
				}
				cur.Store(n)
				emit(map[string]any{"event": "relinked"})
			case "external on":
				cur.Load().SetExternal(true)
			case "external off":
				cur.Load().SetExternal(false)
			default:
				// "volume N": the device's own volume moved (a button, HA).
				var v int
				if _, err := fmt.Sscanf(sc.Text(), "volume %d", &v); err == nil {
					cur.Load().SetVolume(v)
					continue
				}
			case "quit":
				close(quit)
				return
			}
		}
	}()
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, os.Interrupt)

	// The simulated DAC: a steady timeline on its own crystal. The client
	// never sees it directly — it gets a noisy measurement, as the device
	// gets from the ALSA status — while the log reports the truth.
	const frames = 2048
	nominal := time.Duration(frames) * time.Second / 48000
	period := time.Duration(float64(nominal) * (1 - *ppm/1e6))
	out := make([]byte, frames*4)
	start := time.Now()
	k := 0
	status := time.NewTicker(time.Second)
	for {
		select {
		case <-quit:
			cur.Load().Stop("shutdown")
			emit(map[string]any{"event": "stopped"})
			return
		case <-sig:
			cur.Load().Stop("shutdown")
			return
		case <-status.C:
			b, _ := json.Marshal(cur.Load().Status())
			var st map[string]any
			json.Unmarshal(b, &st)
			st["event"] = "status"
			emit(st)
		default:
		}
		dac := start.Add(time.Duration(k) * period).Add(*latency)
		time.Sleep(time.Until(dac.Add(-*latency)))
		measured := dac.Add(time.Duration((rand.Float64()*2 - 1) * float64(*noise)))
		if cur.Load().Fill(out, measured, 0) { // simulated: the read takes no time
			// Where the first frame truly lands, on the server's clock.
			raw := rawUs() + dac.Sub(time.Now()).Microseconds()
			var rms float64
			for i := 0; i < frames; i++ {
				s := float64(int16(binary.LittleEndian.Uint16(out[i*4:])))
				rms += s * s
			}
			emit(map[string]any{
				"event": "period", "rawUs": raw,
				"first": int16(binary.LittleEndian.Uint16(out[0:])),
				"last":  int16(binary.LittleEndian.Uint16(out[(frames-1)*4:])),
				"rms":   int(rms / frames),
			})
		}
		k++
	}
}
