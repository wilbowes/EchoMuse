package codec

import "testing"

// A wrong name here is silence rather than an error, and the two ends failed
// independently: the capture routes leave the ADCs powered down, the playback
// routes leave the DAC powered down, and either alone is a device that looks
// healthy in every log it writes.
func TestRoutesCoverBothEndsOfTheAudioPath(t *testing.T) {
	want := map[string]bool{
		// capture: the DIFFERENTIAL inputs into all four ADCs — the
		// single-ended "IN2" switches beside them are the wrong ones
		"ADC_D Right Ip Select ADC_D DIF1_R switch": true,
		"ADC_D Left Ip Select ADC_D DIF1_L switch":  true,
		"ADC_C Right Ip Select ADC_C DIF1_R switch": true,
		"ADC_C Left Ip Select ADC_C DIF1_L switch":  true,
		"ADC_B Right Ip Select ADC_B DIF1_R switch": true,
		"ADC_B Left Ip Select ADC_B DIF1_L switch":  true,
		"ADC_A Right Ip Select ADC_A DIF1_R switch": true,
		"ADC_A Left Ip Select ADC_A DIF1_L switch":  true,
		// playback: DAC into the output mixer
		"HPR Output Mixer R_DAC Switch": true,
		"HPL Output Mixer L_DAC Switch": true,
	}

	got := map[string]bool{}
	for _, w := range Routes {
		if got[w.Name] {
			t.Errorf("%s listed twice", w.Name)
		}
		if w.Value != "1" {
			t.Errorf("%s: value %q, want \"1\" — every route here is a switch to close", w.Name, w.Value)
		}
		got[w.Name] = true
	}
	for name := range want {
		if !got[name] {
			t.Errorf("missing %s", name)
		}
	}
	for name := range got {
		if !want[name] {
			t.Errorf("unexpected %s", name)
		}
	}
}
