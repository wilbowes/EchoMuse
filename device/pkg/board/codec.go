package board

// Each board's codec state: the routes, input levels, speaker volume and jack
// table, as mixer writes by control NAME (#546). Control ids are positional
// and differ between kernels; writing the wrong one succeeds, and audio is
// silent.
//
// All of it is state Amazon's audio HAL sets on FireOS. It is written on
// FireOS as well, with the values the HAL would set anyway, so a device with
// Android loses nothing and one without it (emOS) gets a working audio path.

// tlvRoutes: biscuit and radar, whose codecs are the same parts.
//
// An ASoC route that is not connected leaves DAPM no reason to power the
// converter at either end of it, so the hardware is powered DOWN rather than
// misrouted (found 2026-09-04 on the first device with no Android: capture
// read rms≈0.00035 on a healthy clock, playback reported underruns=0 into
// silence). Read off the codec, these took every power register to stock's
// value.
//
// CAPTURE: the mics reach the codec on the DIFFERENTIAL inputs. Nothing
// routed DIF1 into any of the four ADCs, so all four sat powered down.
// PLAYBACK: the DAC was not connected to the output mixer.
var tlvRoutes = []Ctl{
	{"ADC_D Right Ip Select ADC_D DIF1_R switch", []string{"1"}},
	{"ADC_D Left Ip Select ADC_D DIF1_L switch", []string{"1"}},
	{"ADC_C Right Ip Select ADC_C DIF1_R switch", []string{"1"}},
	{"ADC_C Left Ip Select ADC_C DIF1_L switch", []string{"1"}},
	{"ADC_B Right Ip Select ADC_B DIF1_R switch", []string{"1"}},
	{"ADC_B Left Ip Select ADC_B DIF1_L switch", []string{"1"}},
	{"ADC_A Right Ip Select ADC_A DIF1_R switch", []string{"1"}},
	{"ADC_A Left Ip Select ADC_A DIF1_L switch", []string{"1"}},

	{"HPR Output Mixer R_DAC Switch", []string{"1"}},
	{"HPL Output Mixer L_DAC Switch", []string{"1"}},
}

// tlvInputGains is the level switch on each ADC channel's differential input,
// written OFF as Amazon's HAL leaves it. Not routes: they set the input's
// level. The kernel's default is On, and an emOS Echo captured about 6dB
// quieter than a FireOS one until these were written (#806).
var tlvInputGains = []Ctl{
	{"ADC_A DIF1_L Input Gain", []string{"0"}},
	{"ADC_A DIF1_R Input Gain", []string{"0"}},
	{"ADC_B DIF1_L Input Gain", []string{"0"}},
	{"ADC_B DIF1_R Input Gain", []string{"0"}},
	{"ADC_C DIF1_L Input Gain", []string{"0"}},
	{"ADC_C DIF1_R Input Gain", []string{"0"}},
	{"ADC_D DIF1_L Input Gain", []string{"0"}},
	{"ADC_D DIF1_R Input Gain", []string{"0"}},
}

// tlvVolume is the DAC digital volume: 0.5dB steps, 127 = 0dB.
var tlvVolume = Volume{Control: "PCM Playback Volume", Mute: "0", Unity: "127"}

const tlvAmp = "Ext_Speaker_Amp_Switch"

// tlvJack: measured 2026-09-03 against a stock FireOS 5.5.5.4 Dot driving the
// same cable, by diffing all 239 controls across an insert. Stock changed
// five; two are left out. Right Channel Only selects a codec channel and our
// wire is mono, so it changes nothing; Ignore Ramp Up was never measured.
//
//   - HP Driver Gain (0..35 = -6..+29dB): something drops it to 0 when a plug
//     goes in, and stock's HAL raises it to 11. Nothing of ours did, so the
//     jack was inaudible. It is Android's HAL reacting to the h2w switch, not
//     accdet, which writes no mixer control.
//   - DAC mux (#566): with a plug in stock reads On, and the jack sat 20-30dB
//     below line level without it (@Kozikodi, FireOS 5.5.5.4, two Dots).
//     Stock leaves it On with the jack empty too; that is not measured on the
//     internal speaker, so removal writes Off, the state every Dot has played
//     its speaker in.
var tlvJack = Jack{
	Insert: []Ctl{
		{tlvAmp, []string{"Off"}},
		{"HP Driver Gain Volume", []string{"11", "11"}},
		{"Audio_DacMux_Setting", []string{"On"}},
	},
	Remove: []Ctl{
		{"HP Driver Gain Volume", []string{"6", "6"}},
		{"Audio_DacMux_Setting", []string{"Off"}},
		{tlvAmp, []string{"On"}},
	},
}

// The Dot 3's codec state, from stock's own /system/vendor/etc/mixer_paths.xml
// on 41C8 (FireOS 6.5.7.5): the initial settings, then the "normal-playback
// speaker" and "normal-playback headphone" paths. The values match what
// tinymix read with stock's mixer running.

// donutRoutes: the AFE interconnects (DL1 to the I2S port that feeds the
// TAS2770, and the TDM capture), and DIF1 into ADC_A and ADC_B, the two
// tlv320aic3101 channels the board uses. ADC_C and ADC_D are not populated.
var donutRoutes = []Ctl{
	{"INT ADDA O03_O04 Switch", []string{"1"}},
	{"I2S O03_O04 Switch", []string{"1"}},
	{"O03 I05 Switch", []string{"1"}},
	{"O04 I06 Switch", []string{"1"}},
	{"O09 I03 Switch", []string{"1"}},
	{"O10 I04 Switch", []string{"1"}},
	{"O05 I05 Switch", []string{"1"}},
	{"O06 I06 Switch", []string{"1"}},
	{"AIN Mux", []string{"INT ADC"}},
	{"ADC_A Left Ip Select ADC_A DIF1_L switch", []string{"1"}},
	{"ADC_A Right Ip Select ADC_A DIF1_R switch", []string{"1"}},
	{"ADC_B Left Ip Select ADC_B DIF1_L switch", []string{"1"}},
	{"ADC_B Right Ip Select ADC_B DIF1_R switch", []string{"1"}},
}

// donutInputGains: stock's mic levels on the two ADCs in use.
var donutInputGains = []Ctl{
	{"ADC_A MICPGA Volume Ctrl", []string{"24", "24"}},
	{"ADC_B MICPGA Volume Ctrl", []string{"24", "24"}},
	{"ADC_A DIF1_L Input Gain", []string{"0"}},
	{"ADC_A DIF1_R Input Gain", []string{"0"}},
	{"ADC_B DIF1_L Input Gain", []string{"0"}},
	{"ADC_B DIF1_R Input Gain", []string{"0"}},
}

// donutVolume is the TAS2770's digital volume. Stock's speaker path writes 255
// and its headphone path 0, which silences the speaker.
var donutVolume = Volume{Control: "PCM Playback Volume", Mute: "0", Unity: "255"}

// donutJack is stock's headphone path and its speaker path back. The TAS2770
// has no amp switch, so its volume is what silences it, first on insert and
// last on removal. "I2S O03_O04 Switch" stays a route: it is decided at
// stream open and this firmware never closes the stream (@shortgame11's
// port found it cannot mute the speaker live). The insert/remove sequence is
// stock's; a plug test on 41C8 is owed.
var donutJack = Jack{
	Insert: []Ctl{
		{"PCM Playback Volume", []string{"0"}},
		{"LINEOUT Mux", []string{"OPEN"}},
		{"Audio Amp Playback Volume", []string{"3", "3"}},
		{"HPOUT Mux", []string{"AUDIO_AMP"}},
	},
	Remove: []Ctl{
		{"HPOUT Mux", []string{"OPEN"}},
		{"Audio Amp Playback Volume", []string{"0", "0"}},
		{"LINEOUT Mux", []string{"VOICE_AMP"}},
		{"PCM Playback Volume", []string{"255"}},
	},
}
