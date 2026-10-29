// bt_audio_probe — drive the firmware's own Bluetooth speaker stack (BR/EDR,
// SDP, AVDTP, SBC, AVRCP) from the console, so a speaker can be tried and its
// cost to WiFi measured before any of it is wired to the dashboard.
//
// /dev/stpbt is single-owner: stop the server first (or turn the BLE proxy
// and Bluetooth audio off for this Echo). Pairings go to the same file the
// server reads, so a speaker paired here is paired for the server too.
// Build with device/tools/build_tools.sh, then:
//
//	bt_audio_probe -discover 10                 # audio sinks in range
//	bt_audio_probe -pair AA:BB:CC:DD:EE:FF      # speaker in pairing mode
//	bt_audio_probe -play AA:BB:CC:DD:EE:FF -seconds 60 -tone 440
//	bt_audio_probe -play AA:BB:CC:DD:EE:FF -wav /data/local/tmp/song.wav
//	bt_audio_probe -forget AA:BB:CC:DD:EE:FF
//
// -play feeds 512-frame periods at 48kHz on a monotonic schedule, the rate
// the write loop hands them over at, and prints the stream state every 2s.
// Read the AP's retries to this Echo (UniFi) beside it, with and without
// -play, to see what the stream costs the WiFi link.
package main

import (
	"encoding/binary"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"os"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bluetooth"
)

const periodFrames = 512

func main() {
	discover := flag.Int("discover", 0, "inquiry for this many seconds and list audio sinks")
	pair := flag.String("pair", "", "pair with this address (speaker in pairing mode)")
	name := flag.String("name", "", "with -pair: the name to keep for it")
	forget := flag.String("forget", "", "forget this pairing")
	play := flag.String("play", "", "stream to this paired address")
	seconds := flag.Int("seconds", 30, "with -play: how long")
	tone := flag.Float64("tone", 440, "with -play: sine frequency, Hz (ignored with -wav)")
	wav := flag.String("wav", "", "with -play: a 48kHz 16-bit PCM WAV, mono or stereo")
	gain := flag.Float64("gain", 0.25, "with -play: tone level, 0-1")
	keys := flag.String("keys", bluetooth.KeyPath, "paired-device file")
	flag.Parse()

	classic := bluetooth.NewClassic(*keys, "EchoMuse probe")
	sc := bluetooth.NewScanner(nil)
	sc.SetClassic(classic)
	var a2dp *bluetooth.A2DP
	if *play != "" {
		a2dp = bluetooth.NewA2DP(classic)
		a2dp.OnButton = func(op string) { log.Printf("speaker button: %s", op) }
	}
	sc.SetAudioEnabled(true)
	defer sc.SetAudioEnabled(false)
	if err := waitUp(classic); err != nil {
		log.Fatal(err)
	}

	switch {
	case *discover > 0:
		found, err := classic.Discover(time.Duration(*discover) * time.Second)
		if err != nil {
			log.Fatal(err)
		}
		for _, r := range found {
			fmt.Printf("%s  %4d dBm  class %06x  %s\n", r.Addr, r.RSSI, r.Class, r.Name)
		}
		fmt.Printf("%d audio sinks\n", len(found))
	case *pair != "":
		start := time.Now()
		if _, err := classic.Pair(*pair, *name); err != nil {
			log.Fatalf("pair: %v", err)
		}
		fmt.Printf("paired with %s in %s\n", *pair, time.Since(start).Round(time.Millisecond))
	case *forget != "":
		if err := classic.Forget(*forget); err != nil {
			log.Fatal(err)
		}
		fmt.Println("forgotten")
	case *play != "":
		src, err := source(*wav, *tone, *gain)
		if err != nil {
			log.Fatal(err)
		}
		stream(a2dp, *play, src, time.Duration(*seconds)*time.Second)
	default:
		for _, p := range classic.Paired() {
			fmt.Printf("%s  %s\n", p.Addr, p.Name)
		}
		fmt.Println("(paired devices; see -h for actions)")
	}
}

// waitUp waits for the session to bring BR/EDR up on the radio.
func waitUp(c *bluetooth.Classic) error {
	deadline := time.Now().Add(10 * time.Second)
	for {
		if c.Running() {
			return nil
		}
		if time.Now().After(deadline) {
			return errors.New("the radio did not come up: is the server holding /dev/stpbt?")
		}
		time.Sleep(200 * time.Millisecond)
	}
}

// source returns a function filling one stereo S16LE period.
func source(path string, freq, gain float64) (func([]byte) bool, error) {
	if path == "" {
		t := 0
		return func(p []byte) bool {
			for i := 0; i < periodFrames; i++ {
				v := int16(gain * 32767 * math.Sin(2*math.Pi*freq*float64(t)/48000))
				t++
				binary.LittleEndian.PutUint16(p[i*4:], uint16(v))
				binary.LittleEndian.PutUint16(p[i*4+2:], uint16(v))
			}
			return true
		}, nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	channels, err := wavHeader(f)
	if err != nil {
		return nil, err
	}
	in := make([]byte, periodFrames*2*channels)
	return func(p []byte) bool {
		if _, err := io.ReadFull(f, in); err != nil {
			return false
		}
		for i := 0; i < periodFrames; i++ {
			if channels == 1 {
				copy(p[i*4:], in[i*2:i*2+2])
				copy(p[i*4+2:], in[i*2:i*2+2])
			} else {
				copy(p[i*4:], in[i*4:i*4+4])
			}
		}
		return true
	}, nil
}

// wavHeader checks for 48kHz 16-bit PCM and leaves f at the sample data.
func wavHeader(f io.Reader) (channels int, err error) {
	var riff [12]byte
	if _, err := io.ReadFull(f, riff[:]); err != nil || string(riff[0:4]) != "RIFF" || string(riff[8:12]) != "WAVE" {
		return 0, errors.New("not a WAV file")
	}
	for {
		var hdr [8]byte
		if _, err := io.ReadFull(f, hdr[:]); err != nil {
			return 0, errors.New("WAV has no data chunk")
		}
		n := int(binary.LittleEndian.Uint32(hdr[4:8]))
		switch string(hdr[0:4]) {
		case "fmt ":
			b := make([]byte, n)
			if _, err := io.ReadFull(f, b); err != nil || n < 16 {
				return 0, errors.New("bad fmt chunk")
			}
			format, ch := binary.LittleEndian.Uint16(b[0:2]), int(binary.LittleEndian.Uint16(b[2:4]))
			rate, bits := binary.LittleEndian.Uint32(b[4:8]), binary.LittleEndian.Uint16(b[14:16])
			if format != 1 || rate != 48000 || bits != 16 || ch < 1 || ch > 2 {
				return 0, fmt.Errorf("need 48kHz 16-bit PCM, got format %d, %dHz, %d-bit, %d channels", format, rate, bits, ch)
			}
			channels = ch
		case "data":
			if channels == 0 {
				return 0, errors.New("data before fmt")
			}
			return channels, nil
		default:
			if _, err := io.CopyN(io.Discard, f, int64(n+n&1)); err != nil {
				return 0, err
			}
		}
	}
}

func stream(a *bluetooth.A2DP, addr string, fill func([]byte) bool, d time.Duration) {
	a.Select(addr, "")
	start := time.Now()
	a.Want(true)
	for !a.Ready() {
		if s := a.Status(); s.State == bluetooth.A2DPOffline || time.Since(start) > 30*time.Second {
			log.Fatalf("could not connect: %s %s", s.State, s.Error)
		}
		time.Sleep(50 * time.Millisecond)
	}
	fmt.Printf("connected in %s: %+v\n", time.Since(start).Round(time.Millisecond), a.Status())

	period := make([]byte, periodFrames*4)
	step := time.Duration(periodFrames) * time.Second / 48000
	next := time.Now()
	report := time.Now().Add(2 * time.Second)
	end := time.Now().Add(d)
	for time.Now().Before(end) {
		if !fill(period) {
			break
		}
		a.Write(period)
		next = next.Add(step)
		if wait := time.Until(next); wait > 0 {
			time.Sleep(wait)
		}
		if time.Now().After(report) {
			s := a.Status()
			fmt.Printf("%5.1fs %-9s %s %dkbps delay %dms dropped %d\n",
				time.Since(start).Seconds(), s.State, s.Codec, s.BitrateK, s.DelayMs, s.Dropped)
			report = report.Add(2 * time.Second)
		}
	}
	time.Sleep(500 * time.Millisecond) // let the queue drain
	fmt.Printf("done: %+v\n", a.Status())
	a.Close()
}
