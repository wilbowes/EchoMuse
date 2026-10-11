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
	"sync"

	"github.com/wilbowes/EchoMuse/internal/bindings/mixer"
)

// Write sets one mixer control, found by name.
type Write struct {
	Name  string
	Value string
}

// Routes is every DAPM switch that must be closed for audio to flow.
//
// By NAME, never by control id (#546). These were ids until 2026-09-17, and on
// the FireOS 6 kernel every one of them named a different control: the eight
// capture writes closed the single-ended IN2 inputs, and the two playback
// writes hit input-mixer switches. Each was a valid write, so nothing failed.
//
// CAPTURE: the microphone array reaches the codec on the DIFFERENTIAL inputs,
// not the single-ended ones. Nothing routed DIF1 into any of the four ADCs, so
// all four sat powered down. The neighbouring "ADC_x DIF1_L/R Input Gain"
// controls are a DIFFERENT thing in the same register block: they do not
// route anything, which is why changing them did nothing for a powered-down
// ADC when they were the first thing tried. They set the input's level, and
// are InputGains below.
//
// PLAYBACK: the DAC was not connected to the output mixer, so it powered down
// with the firmware streaming correctly into it.
var Routes = []Write{
	{"ADC_D Right Ip Select ADC_D DIF1_R switch", "1"},
	{"ADC_D Left Ip Select ADC_D DIF1_L switch", "1"},
	{"ADC_C Right Ip Select ADC_C DIF1_R switch", "1"},
	{"ADC_C Left Ip Select ADC_C DIF1_L switch", "1"},
	{"ADC_B Right Ip Select ADC_B DIF1_R switch", "1"},
	{"ADC_B Left Ip Select ADC_B DIF1_L switch", "1"},
	{"ADC_A Right Ip Select ADC_A DIF1_R switch", "1"},
	{"ADC_A Left Ip Select ADC_A DIF1_L switch", "1"},

	{"HPR Output Mixer R_DAC Switch", "1"},
	{"HPL Output Mixer L_DAC Switch", "1"},
}

// InputGains is the level switch on the differential input each ADC channel
// captures from, written OFF, which is what Amazon's HAL leaves it at.
//
// Nothing wrote these until 2026-10, so a device with no Android kept the
// kernel's default of On and captured about 6dB quieter than one with it
// (#806). Found measuring three Echoes side by side: of 239 mixer controls
// these eight were the only difference between a FireOS 5 Echo and two emOS
// ones, the FireOS one read 3.0-3.8dB louder on every wake, and writing them
// Off on one emOS Echo raised it 5.2dB against the other, with its noise
// floor up about 6dB. The reference dump in device/tools/ shows Off as well.
//
// So this is the same case as Routes: state we were inheriting from the HAL
// without knowing, written here so both userspaces capture at one level. On
// FireOS it is the value already there.
var InputGains = []Write{
	{"ADC_A DIF1_L Input Gain", "0"},
	{"ADC_A DIF1_R Input Gain", "0"},
	{"ADC_B DIF1_L Input Gain", "0"},
	{"ADC_B DIF1_R Input Gain", "0"},
	{"ADC_C DIF1_L Input Gain", "0"},
	{"ADC_C DIF1_R Input Gain", "0"},
	{"ADC_D DIF1_L Input Gain", "0"},
	{"ADC_D DIF1_R Input Gain", "0"},
}

var once sync.Once

// EnsureRoutes applies Routes, then InputGains, exactly once per process.
//
// Called from both the microphone and the speaker Init, because either may run
// first and each needs the routes closed BEFORE it opens its PCM — DAPM decides
// what to power at stream open.
//
// A control that does not resolve is now a loud failure rather than a write to
// whatever happens to hold that id on this kernel.
func EnsureRoutes() {
	once.Do(func() {
		var failed int
		for _, w := range Routes {
			if err := mixer.Set(w.Name, w.Value); err != nil {
				failed++
			}
		}
		if failed > 0 {
			log.Printf("[codec] %d of %d DAPM routes failed — audio may be silent",
				failed, len(Routes))
		} else {
			log.Printf("[codec] %d DAPM routes closed", len(Routes))
		}
		// Separately counted: a route that fails is silence, an input gain
		// that fails is a microphone 6dB down, and the log should say which.
		failed = 0
		for _, w := range InputGains {
			if err := mixer.Set(w.Name, w.Value); err != nil {
				failed++
			}
		}
		if failed > 0 {
			log.Printf("[codec] %d of %d input gains failed — microphones may read about 6dB low",
				failed, len(InputGains))
		} else {
			log.Printf("[codec] %d input gains set", len(InputGains))
		}
	})
}
