// echoref_replay feeds a bench raw capture (rawtap_bench.go: <base>.raw mics,
// .lb loopback, .ts arrival order) through internal/echoref in the order the
// chunks arrived, and prints every lock, move and loss with its time. For
// checking the aligner against a real board offline.
//
//	go run ./tools/echoref_replay /path/to/rawcap-1794028672
//
// The capture is the donut's: 4ch S32LE microphones at 16 kHz (ch0 used),
// 48 kHz stereo S16LE loopback.
package main

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"os"
	"strings"

	"github.com/wilbowes/EchoMuse/internal/echoref"
)

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: echoref_replay <capture base>")
		os.Exit(2)
	}
	base := os.Args[1]
	mic, err1 := os.ReadFile(base + ".raw")
	lb, err2 := os.ReadFile(base + ".lb")
	ts, err3 := os.Open(base + ".ts")
	if err1 != nil || err2 != nil || err3 != nil {
		fmt.Fprintln(os.Stderr, err1, err2, err3)
		os.Exit(1)
	}
	a := echoref.New()
	var micOff, lbOff int
	var micIndex int64
	var lastOffset int64
	wasLocked := false
	sc := bufio.NewScanner(ts)
	for sc.Scan() {
		var tag string
		var at, n int
		if _, err := fmt.Sscan(strings.TrimSpace(sc.Text()), &tag, &at, &n); err != nil {
			continue
		}
		switch tag {
		case "l":
			if lbOff+n > len(lb) {
				break
			}
			a.PushLoopback(lb[lbOff : lbOff+n])
			lbOff += n
		case "m":
			if micOff+n > len(mic) {
				break
			}
			frames := n / 16
			m := make([]int16, frames)
			for i := range m {
				m[i] = int16(int32(binary.LittleEndian.Uint32(mic[micOff+16*i:])) >> 16)
			}
			micOff += n
			a.Observe(micIndex, m)
			ref := a.Take(micIndex, len(m))
			sec := float64(micIndex) / echoref.Rate
			off := a.Offset()
			switch {
			case a.Locked() && !wasLocked:
				fmt.Printf("%6.1fs locked, offset %d (%.1f ms)\n", sec, off, float64(off)/16)
			case !a.Locked() && wasLocked:
				fmt.Printf("%6.1fs lost\n", sec)
			case a.Locked() && off != lastOffset:
				fmt.Printf("%6.1fs moved %+d samples (%+.1f ms)\n", sec, off-lastOffset, float64(off-lastOffset)/16)
			}
			_ = ref
			wasLocked, lastOffset = a.Locked(), off
			micIndex += int64(frames)
		}
	}
	fmt.Printf("searches %d, locks %d, moves %d, misses %d, losses %d\n",
		a.Searches, a.Locks, a.Moves, a.Misses, a.Losses)
}
