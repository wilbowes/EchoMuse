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

// Bench-only raw capture, for the AEC harness: every raw capture batch
// written to disk exactly as the pipeline receives it, and on a board with a
// separate loopback PCM (the donut) every loopback chunk beside it, with
// each chunk's arrival time so the two can be aligned offline.
//
// Triggered by a file, not a signal: SIGUSR2 would kill a release binary.
//
//	echo 60 > /data/local/tmp/em-rawcap-request
//
// writes /data/local/tmp/rawcap-<unix>.raw (mics, the board's own capture
// format), .lb (loopback, if any) and .ts ("m|l <monotonic ns> <bytes>" per
// chunk) for that many seconds (max 120). The audio goroutines never wait on
// the disk: chunks go through buffered channels and are dropped, and
// counted, if the writer falls behind.

const (
	rawcapRequest = "/data/local/tmp/em-rawcap-request"
	rawcapDir     = "/data/local/tmp"
	rawcapMaxSecs = 120
)

type rawChunk struct {
	loop bool
	at   int64
	b    []byte
}

var rawStart = time.Now()

func init() {
	ch := make(chan rawChunk, 256)
	var active atomic.Bool
	var dropped atomic.Int64
	push := func(loop bool, b []byte) {
		if !active.Load() {
			return
		}
		c := rawChunk{loop: loop, at: int64(time.Since(rawStart)), b: append([]byte(nil), b...)}
		select {
		case ch <- c:
		default:
			dropped.Add(1)
		}
	}
	rawTap = func(b []byte) { push(false, b) }
	rawLoopTap = func(b []byte) { push(true, b) }
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
			base := fmt.Sprintf("%s/rawcap-%d", rawcapDir, time.Now().Unix())
			mf, err1 := os.Create(base + ".raw")
			lf, err2 := os.Create(base + ".lb")
			tf, err3 := os.Create(base + ".ts")
			if err1 != nil || err2 != nil || err3 != nil {
				log.Printf("[rawcap] create: %v %v %v", err1, err2, err3)
				continue
			}
			log.Printf("[rawcap] recording %ds to %s.{raw,lb,ts}", secs, base)
			dropped.Store(0)
			active.Store(true)
			var mic, lb int64
			write := func(c rawChunk) {
				tag := "m"
				if c.loop {
					tag = "l"
					n, _ := lf.Write(c.b)
					lb += int64(n)
				} else {
					n, _ := mf.Write(c.b)
					mic += int64(n)
				}
				fmt.Fprintf(tf, "%s %d %d\n", tag, c.at, len(c.b))
			}
			deadline := time.After(time.Duration(secs) * time.Second)
		loop:
			for {
				select {
				case c := <-ch:
					write(c)
				case <-deadline:
					break loop
				}
			}
			active.Store(false)
			for len(ch) > 0 {
				write(<-ch)
			}
			mf.Close()
			lf.Close()
			tf.Close()
			log.Printf("[rawcap] done: %s, mic %d bytes, loopback %d bytes, %d chunks dropped",
				base, mic, lb, dropped.Load())
		}
	}()
}
