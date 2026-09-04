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
//	          stock   ours     meaning
//	  0012      85      05     NADC clock divider, bit7 = powered
//	  0013      83      03     MADC clock divider
//	  0026      11      00     ADC flags: left+right converting
//	  003f      d6      16     DAC data path, bit7/6 = left/right powered
//	  0089      30      00     output driver power
//	  008c/8d   08      00     HPL/HPR output mixer routing
//
// Applying the routes below took every one of those to stock's exact value.
//
// This is written unconditionally, on FireOS as well, and that is deliberate:
// the values are the ones the HAL would set anyway, so a device that still has
// Android loses nothing, and one that does not gains a working audio path. The
// point of the project is not to need Amazon's userspace, and inheriting
// hardware state from it is a dependency whether or not it currently holds.
package codec

import (
	"log"
	"os/exec"
	"strings"
	"sync"
)

// Write is one `tinymix -D 0 <ctl> <value>` invocation.
type Write struct {
	Ctl   string
	Value string
	Name  string // the control's mixer name, for the log line and for grep
}

// Routes is every DAPM switch that must be closed for audio to flow.
//
// Control IDs are positional and specific to this board, like the ones in the
// speaker package's jack routing — the name is carried alongside so a mismatch
// is greppable rather than a bare number nobody can check.
//
// CAPTURE: the microphone array reaches the codec on the DIFFERENTIAL inputs,
// not the single-ended ones. Nothing routed DIF1 into any of the four ADCs, so
// all four sat powered down. Note the neighbouring "ADC_x DIF1_L/R Input Gain"
// controls are a DIFFERENT thing in the same register block and were the first
// thing tried; changing them does nothing, and they are not listed here.
//
// PLAYBACK: the DAC was not connected to the output mixer, so it powered down
// with the firmware streaming correctly into it.
var Routes = []Write{
	{"170", "1", "ADC_D Right Ip Select ADC_D DIF1_R switch"},
	{"177", "1", "ADC_D Left Ip Select ADC_D DIF1_L switch"},
	{"184", "1", "ADC_C Right Ip Select ADC_C DIF1_R switch"},
	{"191", "1", "ADC_C Left Ip Select ADC_C DIF1_L switch"},
	{"200", "1", "ADC_B Right Ip Select ADC_B DIF1_R switch"},
	{"207", "1", "ADC_B Left Ip Select ADC_B DIF1_L switch"},
	{"216", "1", "ADC_A Right Ip Select ADC_A DIF1_R switch"},
	{"223", "1", "ADC_A Left Ip Select ADC_A DIF1_L switch"},

	{"234", "1", "HPR Output Mixer R_DAC Switch"},
	{"237", "1", "HPL Output Mixer L_DAC Switch"},
}

var once sync.Once

// EnsureRoutes applies Routes exactly once per process.
//
// Called from both the microphone and the speaker Init, because either may run
// first and each needs the routes closed BEFORE it opens its PCM — DAPM decides
// what to power at stream open. The sync.Once is what makes calling it from
// both sites free: process spawns are not cheap on this hardware (a heavy shell
// command was observed inducing mic capture stalls, and the mic pipeline has a
// hard 160ms deadline), so ten of them must not become twenty.
func EnsureRoutes() {
	once.Do(func() {
		var failed int
		for _, w := range Routes {
			out, err := exec.Command("tinymix", "-D", "0", w.Ctl, w.Value).CombinedOutput()
			if err != nil {
				failed++
				log.Printf("[codec] route ctl %s (%s): %v — %s",
					w.Ctl, w.Name, err, strings.TrimSpace(string(out)))
			}
		}
		if failed > 0 {
			log.Printf("[codec] %d of %d DAPM routes failed — audio may be silent",
				failed, len(Routes))
		}
	})
}
