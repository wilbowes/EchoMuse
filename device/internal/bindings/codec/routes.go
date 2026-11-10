// Package codec brings up the DAPM routes the audio path needs, instead of
// inheriting them from Amazon's audio HAL.
//
// Until 2026-09-04 nothing here existed, because nothing had to: on FireOS the
// HAL configures the codec long before our process opens a PCM, so both the
// microphone array and the speaker worked and we never learned we were relying
// on it. Running EchoMuse on a device with no Android userspace made the
// dependency visible in the least helpful way available — capture returned a
// steady rms≈0.00035 with a perfectly healthy ALSA clock (300.6s of audio over
// 300.3s of wall, zero stalls), and playback reported
// "voice stream complete, periods=35 underruns=0" while the room stayed silent.
//
// Both halves had the same cause. An ASoC route that is not connected leaves
// DAPM no reason to power the converter at either end of it, so the hardware is
// powered DOWN rather than merely misrouted. Read off the codec (i2c 2-0018) on
// a device with no Android, against a stock FireOS Dot running the same
// firmware:
//
//	        stock   ours     meaning
//	0012      85      05     NADC clock divider, bit7 = powered
//	0013      83      03     MADC clock divider
//	0026      11      00     ADC flags: left+right converting
//	003f      d6      16     DAC data path, bit7/6 = left/right powered
//	0089      30      00     output driver power
//	008c/8d   08      00     HPL/HPR output mixer routing
//
// Writing the board's routes (pkg/board) took every one of those to stock's
// exact value.
//
// This is written unconditionally, on FireOS as well, and that is deliberate:
// the values are the ones the HAL would set anyway, so a device that still has
// Android loses nothing, and one that does not gains a working audio path. The
// point of the project is not to need Amazon's userspace, and inheriting
// hardware state from it is a dependency whether or not it currently holds.
package codec

import (
	"log"
	"sync"

	"github.com/wilbowes/EchoMuse/internal/bindings/mixer"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

var once sync.Once

// EnsureRoutes writes the board's routes, then its input gains, exactly once
// per process.
//
// Called from both the microphone and the speaker Init, because either may run
// first and each needs the routes closed BEFORE it opens its PCM — DAPM decides
// what to power at stream open.
func EnsureRoutes() {
	once.Do(func() {
		l := board.CurrentLayout()
		apply(l.Routes, l.InputGains)
	})
}

// apply writes each table and counts what failed. A control that does not
// resolve is a loud failure rather than a write to whatever holds that id on
// this kernel (#546).
func apply(routes, gains []board.Ctl) {
	if failed := write(routes); failed > 0 {
		log.Printf("[codec] %d of %d DAPM routes failed — audio may be silent",
			failed, len(routes))
	} else {
		log.Printf("[codec] %d DAPM routes closed", len(routes))
	}
	// Separately counted: a route that fails is silence, an input gain that
	// fails is a microphone at the wrong level, and the log should say which.
	if failed := write(gains); failed > 0 {
		log.Printf("[codec] %d of %d input gains failed — microphones may read at the wrong level",
			failed, len(gains))
	} else {
		log.Printf("[codec] %d input gains set", len(gains))
	}
}

func write(ctls []board.Ctl) (failed int) {
	for _, c := range ctls {
		if err := mixer.Set(c.Name, c.Values...); err != nil {
			failed++
		}
	}
	return failed
}
