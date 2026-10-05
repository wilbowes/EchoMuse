//go:build server && bench

package mic

import (
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// Bench-only raw capture, for the AEC harness: every raw 9-channel batch
// (S24_3LE, 16kHz; ch0-6 mics, ch7/ch8 the playback loopback) written to
// disk exactly as the pipeline receives it.
//
// Triggered by a file, not a signal: SIGUSR2 would kill a release binary.
//
//	echo 60 > /data/local/tmp/em-rawcap-request
//
// writes /data/local/tmp/rawcap-<unix>.s24 for that many seconds (max 120).
// The mic goroutine never waits on the disk: batches go through a buffered
// channel and are dropped, and counted, if the writer falls behind.

const (
	rawcapRequest = "/data/local/tmp/em-rawcap-request"
	rawcapDir     = "/data/local/tmp"
	rawcapMaxSecs = 120
)

func init() {
	ch := make(chan []byte, 64)
	var active atomic.Bool
	var dropped atomic.Int64
	rawTap = func(b []byte) {
		if !active.Load() {
			return
		}
		select {
		case ch <- b:
		default:
			dropped.Add(1)
		}
	}
	go func() {
		for {
			time.Sleep(500 * time.Millisecond)
			raw, err := os.ReadFile(rawcapRequest)
			if err != nil {
				continue
			}
			os.Remove(rawcapRequest)
			secs, err := strconv.Atoi(strings.TrimSpace(string(raw)))
			if err != nil || secs < 1 || secs > rawcapMaxSecs {
				log.Printf("[rawcap] bad request %q (1-%d seconds)", raw, rawcapMaxSecs)
				continue
			}
			path := fmt.Sprintf("%s/rawcap-%d.s24", rawcapDir, time.Now().Unix())
			f, err := os.Create(path)
			if err != nil {
				log.Printf("[rawcap] %v", err)
				continue
			}
			log.Printf("[rawcap] recording %ds to %s", secs, path)
			dropped.Store(0)
			active.Store(true)
			var written int64
			deadline := time.After(time.Duration(secs) * time.Second)
		loop:
			for {
				select {
				case b := <-ch:
					n, _ := f.Write(b)
					written += int64(n)
				case <-deadline:
					break loop
				}
			}
			active.Store(false)
			for len(ch) > 0 {
				n, _ := f.Write(<-ch)
				written += int64(n)
			}
			f.Close()
			log.Printf("[rawcap] done: %s, %d bytes (%.1fs), %d batches dropped",
				path, written, float64(written)/(16000*27), dropped.Load())
		}
	}()
}
