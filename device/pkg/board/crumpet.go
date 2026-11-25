package board

// Crumpet is this PVT unit's actual factory identity, read back from /proc/idme.
// The PCM/input names and four-channel S32 stream were measured on its stock
// 4.4.22 kernel in RAM on 2026-10-29. No thermal or GPIO fallback is stated.
var Crumpet = &Board{
	ID:           "crumpet",
	DeviceTypeID: "A1RABVCI4QCIKC",
	Hardware:     crumpetHardware,
}

var crumpetHardware = &Hardware{
	Keys:    []Input{{Name: "gpio-keys"}, {Name: "gpio-privacy"}},
	LEDRing: I2C{Driver: "is31fl3236"},
	// LED 0 is 210 degrees clockwise from volume-up (measured on Dot3New).
	RingLED0: 210,
	Capture:  PCM{Name: "TDM_Capture"},
	Playback: PCM{Name: "DL1_Playback"},
	// Four-channel tap test on this C78MP8, 2026-10-30: ch0 by plus,
	// ch1 by action, ch2 by minus, ch3 by mute, matching donut's layout.
	Mics:         donutMics,
	Loopback:     PCM{Name: "DL1_AWB_Record"},
	LoopbackRate: 48000,
	LoopbackChs:  2,
	MicMutes: []string{
		"ADC_A Left Mute", "ADC_A Right Mute",
		"ADC_B Left Mute", "ADC_B Right Mute",
	},
	MicADCs: donutMicADCs,
	// These control names/values match this unit's original mixer_paths.xml
	// and were applied successfully before the measured capture.
	Routes:     donutRoutes,
	InputGains: donutInputGains,
	Speaker: Speaker{
		Volume: donutVolume,
		Jack:   donutJack,
		Setup:  donutHardware.Speaker.Setup,
		// Original NS65741-8149 MBCL.cfg, first band: 70 Hz, -50 dB.
		BassGuard: BassBand{CrossoverHz: 70, ThresholdDb: -50},
	},
}
