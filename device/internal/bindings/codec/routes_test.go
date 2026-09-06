package codec

import "testing"

// The routes are a table of measured control ids, and a typo in one of them is
// silence rather than an error — the same reason the jack routing table is
// pinned. Both ends are covered because they failed independently: the capture
// routes leave the ADCs powered down, the playback routes leave the DAC powered
// down, and either alone is a device that looks healthy in every log it writes.
func TestRoutesCoverBothEndsOfTheAudioPath(t *testing.T) {
	want := map[string]string{
		// capture: DIF1 into all four ADCs, left and right
		"170": "ADC_D Right Ip Select ADC_D DIF1_R switch",
		"177": "ADC_D Left Ip Select ADC_D DIF1_L switch",
		"184": "ADC_C Right Ip Select ADC_C DIF1_R switch",
		"191": "ADC_C Left Ip Select ADC_C DIF1_L switch",
		"200": "ADC_B Right Ip Select ADC_B DIF1_R switch",
		"207": "ADC_B Left Ip Select ADC_B DIF1_L switch",
		"216": "ADC_A Right Ip Select ADC_A DIF1_R switch",
		"223": "ADC_A Left Ip Select ADC_A DIF1_L switch",
		// playback: DAC into the output mixer
		"234": "HPR Output Mixer R_DAC Switch",
		"237": "HPL Output Mixer L_DAC Switch",
	}

	got := map[string]string{}
	for _, w := range Routes {
		if _, dup := got[w.Ctl]; dup {
			t.Errorf("control %s listed twice", w.Ctl)
		}
		if w.Value != "1" {
			t.Errorf("control %s (%s): value %q, want \"1\" — every route here is a switch to close",
				w.Ctl, w.Name, w.Value)
		}
		got[w.Ctl] = w.Name
	}

	for ctl, name := range want {
		if got[ctl] != name {
			t.Errorf("control %s: name %q, want %q", ctl, got[ctl], name)
		}
	}
	for ctl := range got {
		if _, ok := want[ctl]; !ok {
			t.Errorf("unexpected control %s (%s)", ctl, got[ctl])
		}
	}
}
