// Command profile_smoke exercises the profile-driven bindings against real
// hardware: it verifies the profile against the driver, captures from the mic,
// plays a tone through the speaker, and reports whether the microphones and
// the AEC reference hear it.
//
// It restores the device on exit (restarting any Android audio services it
// stopped, putting the amp back), so a bring-up run does not leave the system
// silent.
//
//	adb push profile_smoke /data/local/tmp/ && adb shell /data/local/tmp/profile_smoke
package main

import (
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/wilbowes/EchoMuse/internal/alsa"
	"github.com/wilbowes/EchoMuse/internal/beamformer"
	bled "github.com/wilbowes/EchoMuse/internal/bindings/led"
	bmic "github.com/wilbowes/EchoMuse/internal/bindings/mic"
	bspk "github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/profile"
)

func main() {
	name := flag.String("profile", "", "force a profile instead of autodetecting")
	tone := flag.Float64("tone", 1000, "test tone frequency in Hz")
	amp := flag.Float64("amp", 0.2, "test tone amplitude, 0..1")
	secs := flag.Float64("seconds", 3, "capture duration")
	captureOnly := flag.Bool("capture-only", false,
		"microphones only; makes no sound. Use for long soak runs where the "+
			"audio-vs-wall-clock ledger is the thing being measured.")
	flag.Parse()

	log.SetFlags(0)

	prof := profile.Detect()
	if *name != "" {
		if p := profile.ByName(*name); p != nil {
			prof = p
		} else {
			log.Fatalf("unknown profile %q (have %v)", *name, profile.Names())
		}
	}
	fmt.Printf("profile: %s\n", prof.Name)
	fmt.Printf("  mic:     card %d dev %d  %dch %v @%dHz  period %d x %d\n",
		prof.Mic.Card, prof.Mic.Device, prof.Mic.Channels, prof.Mic.Format,
		prof.Mic.SampleRate, prof.Mic.PeriodSize, prof.Mic.Periods)
	fmt.Printf("  speaker: card %d dev %d  %dch %v @%dHz  period %d x %d\n",
		prof.Speaker.Card, prof.Speaker.Device, prof.Speaker.Channels, prof.Speaker.Format,
		prof.Speaker.SampleRate, prof.Speaker.PeriodSize, prof.Speaker.Periods)
	fmt.Printf("  mics on channels %v, AEC reference %v\n\n",
		prof.Mic.MicChannels, prof.Mic.RefChannels)

	// --- verify against the driver before touching anything ---
	fmt.Println("== verify profile against driver ==")
	if err := prof.Verify(); err != nil {
		log.Fatalf("FAIL: %v", err)
	}
	fmt.Println("ok: profile matches driver-reported capabilities")

	// --- take ownership, and make sure we give it back ---
	restore := func() {
		prof.SilenceAmp()
		if len(prof.StopServices) > 0 {
			prof.ReleaseServices()
			fmt.Println("\nrestored: amp reset, audio services restarted")
			return
		}
		fmt.Println("\nrestored: amp reset (this profile stops no services)")
	}
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() { <-sig; restore(); os.Exit(1) }()
	defer restore()

	if err := prof.Prepare(); err != nil {
		// Not Fatalf: that skips the deferred restore and leaves the device in
		// the state this tool promises to undo.
		fmt.Printf("prepare failed: %v\n", err)
		return
	}

	// --- LED backend ---
	fmt.Println("\n== LED backend ==")
	var ledCtl = pickLED(prof)
	if err := ledCtl.Init(); err != nil {
		log.Fatalf("led init: %v", err)
	}
	n, _ := ledCtl.GetNumLEDs()
	fmt.Printf("ok: %d LEDs\n", n)

	// --- mic ---
	fmt.Println("\n== microphone ==")
	mic := bmic.NewProfileMicrophone(prof)
	if err := mic.Init(); err != nil {
		log.Fatalf("mic init: %v", err)
	}
	defer mic.Close()

	fmt.Printf("baseline (%.1fs, silence expected)...\n", *secs)
	base := measure(mic, prof, *secs)
	report(prof, base)

	if *captureOnly {
		fmt.Println("\ncapture-only: skipping the speaker test (no sound emitted)")
		return
	}

	// --- speaker ---
	fmt.Println("\n== speaker ==")
	// nil taps: this tool measures acoustically via the mics, so it needs
	// neither the AEC far-end feed nor the LED level meter.
	spk := bspk.NewProfileSpeaker(prof, nil, nil)
	if err := spk.Init(); err != nil {
		log.Fatalf("speaker init: %v", err)
	}
	defer spk.Close()

	stop := make(chan struct{})
	go pumpTone(spk, prof, *tone, *amp, stop)
	time.Sleep(700 * time.Millisecond) // let the prime fill

	fmt.Printf("capturing while playing %.0f Hz...\n", *tone)
	withTone := measure(mic, prof, *secs)
	close(stop)
	report(prof, withTone)

	// --- verdict ---
	fmt.Println("\n== verdict ==")
	micCh := prof.Mic.MicChannels[0]
	gain := withTone[micCh].rms - base[micCh].rms
	fmt.Printf("mic ch%d rose %.1f dB while the tone played\n", micCh, gain)
	if gain > 10 {
		fmt.Println("PASS: microphones hear the speaker (playback and capture both work)")
	} else {
		fmt.Println("FAIL: no acoustic pickup; speaker output is not reaching the mics")
	}
	if prof.HasAECReference() {
		refCh := prof.Mic.RefChannels[0]
		rg := withTone[refCh].rms - base[refCh].rms
		fmt.Printf("AEC reference ch%d rose %.1f dB\n", refCh, rg)
		if rg > 20 {
			fmt.Println("PASS: hardware AEC reference is live")
		} else {
			fmt.Println("FAIL: AEC reference channel stayed silent")
		}
	}

	// --- beamformer bypass ---
	fmt.Println("\n== beamformer bypass ==")
	bp := beamformer.NewBypass(prof.Mic.Channels, prof.Mic.MicChannels[0])
	raw := make([]byte, prof.Mic.PeriodSize*prof.Mic.FrameBytes())
	mono, angle := bp.Process(raw, 0, 1.0)
	fmt.Printf("ok: %d bytes mono S16 from a %d byte period, angle %v\n",
		len(mono), len(raw), angle)
}

func pickLED(p *profile.Profile) interface {
	Init() error
	GetNumLEDs() (int, error)
} {
	if p.HasLEDRing {
		// The I2C ring controller is only valid on devices that have one.
		return &bled.I2CController{}
	}
	return bled.NewNullController()
}

type level struct {
	peak int32
	rms  float64
}

// measure captures for the given duration and returns per-channel levels.
func measure(m *bmic.ProfileMicrophone, p *profile.Profile, secs float64) []level {
	ch := m.Subscribe()
	defer m.Unsubscribe(ch)

	out := make([]level, p.Mic.Channels)
	sumsq := make([]float64, p.Mic.Channels)
	var frames int64

	deadline := time.After(time.Duration(secs * float64(time.Second)))
	buf := make([]int32, p.Mic.PeriodSize)
	for {
		select {
		case <-deadline:
			for c := range out {
				if frames > 0 {
					out[c].rms = dbfs(math.Sqrt(sumsq[c] / float64(frames)))
				} else {
					out[c].rms = -144
				}
			}
			return out
		case period, ok := <-ch:
			if !ok {
				return out
			}
			for c := 0; c < p.Mic.Channels; c++ {
				n := alsa.ExtractS24_3LE(period, p.Mic.Channels, c, buf)
				for i := 0; i < n; i++ {
					v := buf[i]
					a := v
					if a < 0 {
						a = -a
					}
					if a > out[c].peak {
						out[c].peak = a
					}
					sumsq[c] += float64(v) * float64(v)
				}
				if c == 0 {
					frames += int64(n)
				}
			}
		}
	}
}

func report(p *profile.Profile, l []level) {
	for c := 0; c < p.Mic.Channels; c++ {
		role := "unused"
		for _, m := range p.Mic.MicChannels {
			if m == c {
				role = "mic"
			}
		}
		for _, r := range p.Mic.RefChannels {
			if r == c {
				role = "aec-ref"
			}
		}
		fmt.Printf("  ch%d %-8s peak %8d  rms %7.1f dBFS\n", c, role, l[c].peak, l[c].rms)
	}
}

// pumpTone feeds a mono sine through the Speaker interface, exercising the
// same PumpPeriod path the server uses.
func pumpTone(s *bspk.ProfileSpeaker, p *profile.Profile, freq, amp float64, stop <-chan struct{}) {
	sampleBytes := p.Speaker.Format.Bytes()
	mono := make([]byte, p.Speaker.PeriodSize*sampleBytes)
	phase := 0.0
	step := 2 * math.Pi * freq / float64(p.Speaker.SampleRate)

	for {
		select {
		case <-stop:
			s.EndStream()
			return
		default:
		}
		for i := 0; i < p.Speaker.PeriodSize; i++ {
			v := int16(math.Sin(phase) * amp * 32767)
			phase += step
			mono[i*2] = byte(v)
			mono[i*2+1] = byte(v >> 8)
		}
		if err := s.PumpPeriod(mono); err != nil {
			// Losing the playback loop during shutdown is expected: Close()
			// tears it down while this goroutine may still be mid-send.
			select {
			case <-stop:
			default:
				log.Printf("pump: %v", err)
			}
			return
		}
	}
}

func dbfs(v float64) float64 {
	if v <= 0 {
		return -144
	}
	return 20 * math.Log10(v/8388608.0)
}
