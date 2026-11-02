package board

// Voicing is how a board's speaker is voiced (#229): a correction for its
// driver, three compressor bands above the bass guard, a filter on the voice
// plane, and how far full volume drives the chain. Every number is measured
// on that board's own driver and chosen by ear against it, so a board nobody
// has measured has none and plays through the plain chain.
//
// The Python reference is em_eq / em_mbc / em_volume, held to biscuit's
// numbers by the output chain's vectors.
type Voicing struct {
	Correction []Peak // after the user's EQ
	Shelf      Shelf
	// HighpassHz (LR4) and Notch sit under the correction, where its peaks
	// would otherwise boost what the driver cannot play.
	HighpassHz float64
	Notch      Peak

	Crossovers [2]float64 // Hz, between the three upper bands
	Upper      [3]BandLaw // low to high, above the bass guard's crossover
	// UpperFloorDb is the most any upper band takes off; UpperHoldMs holds a
	// reduction before it releases, at UpperReleaseMs per 10dB.
	UpperFloorDb, UpperHoldMs, UpperReleaseMs float64
	// The bass guard under the voicing: the deeper of BassFloorDb and the
	// user's bassGuardDb, held BassHoldMs. Without the holds the gain
	// recovers between a waveform's peaks and is cut on the next.
	BassFloorDb, BassHoldMs float64

	Voice []Peak // the voice plane's own filter, before the mix

	// ByVolume are filters whose gain follows the volume, after the
	// correction. Empty for a board whose stock EQ holds one shape.
	ByVolume []VolumeEQ

	// CeilingDb is full volume's gain into the chain: the plain law
	// (0.5dB per level, unity at 127) lifted by this much, so the top of the
	// range drives the compressors rather than the DAC.
	CeilingDb float64
}

// Peak is a peaking biquad.
type Peak struct{ Hz, Db, Q float64 }

// Shelf is a high shelf, slope 1.
type Shelf struct{ Hz, Db float64 }

// VolumeEQ is a peaking filter whose gain follows the volume, as stock's EQ
// does by swapping curves at volume boundaries. Gains are anchored at the
// middle of each stock band and interpolated between, so a button press
// moves the tone a little rather than stepping it at a boundary.
type VolumeEQ struct {
	Hz, Q float64
	Gains []VolumeGain // ascending Percent; held flat past either end
}

// VolumeGain is the filter's gain at one volume, in percent of full.
type VolumeGain struct{ Percent, Db float64 }

// GainAt is the filter's gain at a volume in percent.
func (v VolumeEQ) GainAt(percent float64) float64 {
	g := v.Gains
	if len(g) == 0 {
		return 0
	}
	if percent <= g[0].Percent {
		return g[0].Db
	}
	for i := 1; i < len(g); i++ {
		if percent <= g[i].Percent {
			t := (percent - g[i-1].Percent) / (g[i].Percent - g[i-1].Percent)
			return g[i-1].Db + t*(g[i].Db-g[i-1].Db)
		}
	}
	return g[len(g)-1].Db
}

// BandLaw is a compressor band's static law and attack.
type BandLaw struct{ Ratio, ThresholdDb, AttackMs float64 }

// biscuitVoicing: fitted to VVV's driver (#247 sweep, 2026-10-16) and picked
// by ear on 2026-10-18; the volume ceiling chosen on 2026-10-19.
var biscuitVoicing = &Voicing{
	Correction: []Peak{
		{174.9, 26.13, 0.388},
		{3277.5, -10.94, 0.753},
		{576.3, -10.93, 0.735},
		{8707.8, 5.52, 2.985},
	},
	Shelf:      Shelf{11874.4, 12.0},
	HighpassHz: 80,
	Notch:      Peak{65, -25, 4},
	Crossovers: [2]float64{500, 7500},
	// The first band is harder than stock's 2:1 from -10, because this
	// driver's 2nd harmonic at 150-300Hz rises with level.
	Upper:          [3]BandLaw{{3, -16, 20}, {2, -10, 10}, {2, -10, 10}},
	UpperFloorDb:   -40,
	UpperHoldMs:    30,
	UpperReleaseMs: 100,
	BassFloorDb:    -40,
	BassHoldMs:     20,
	Voice: []Peak{
		{228.0, 3.0, 0.76},
		{2105.5, -2.8, 1.25},
		{8583.1, -5.17, 5.0},
	},
	CeilingDb: 10,
}

// radarVoicing is a CANDIDATE, not yet heard: fitted on 2026-10-24 to 07MU's
// own-mic sweep (one placement, 40Hz-7.5kHz) where it agrees with radar's
// stock EQ (200Hz, 2-3.15kHz), with the bass lift capped near stock's at
// 70-80% volume. The bands are radar's own MBCL crossovers and laws (70/200/
// 3250Hz; 20:1 from -25 is the bass guard, then 10:1 -18, 3:1 -15, 2:1 -10),
// because the driver's 3rd harmonic at 70Hz reaches -3.7dB at -6dBFS. The
// ceiling follows stock's +4 input and +3 trim. No voice filter yet.
//
// ByVolume is stock's own change with volume (EQ_50..EQ_100, boundaries 50/
// 60/70/80/100%), each fitted as one peak shared across curves: bass at
// 71.7Hz (12.0/9.1/6.8/4.5/0dB re EQ_100, 1.5dB RMS 50-400Hz at the worst
// curve), zeroed at 75% where the fixed correction already has stock's
// lift; and 2.8-3.7kHz (+4.35 at 50-60%, +2.4 at 70%, 1.0dB RMS). #1077
// measured the same two steps acoustically on a stock radar.
//
// 2026-11-02 A/B against stock 07MU (Yeti at 30cm, pink noise at 30/60/90%):
// 8-12kHz sat 1-2.4dB above stock, so a narrow 10.5kHz cut (a first try at
// 9.8kHz, Q1.2 also took 1dB out of 5-8kHz, which already matched); and both
// volume-following steps fell 1-2dB short of stock's, so the bass gains
// below 75% rise 1.5dB and the 3.3kHz ones 1dB. After the bass and 3.3kHz
// change the 60% step matched stock within 1.2dB; the 30% step is unmeasured
// (pink noise 5dB above the room at the mic). Wil: "richer at high volume".
var radarVoicing = &Voicing{
	Correction: []Peak{
		{64.7, 14.0, 0.90},
		{221.3, -4.5, 1.00},
		{697.4, -2.46, 3.00},
		{2345.5, -6.0, 2.29},
		{3133.0, 3.48, 3.00},
		{10500.0, -4.5, 2.50},
	},
	Shelf:          Shelf{10000, 0},
	HighpassHz:     40,
	Notch:          Peak{40, 0, 1},
	Crossovers:     [2]float64{200, 3250},
	Upper:          [3]BandLaw{{10, -18, 20}, {3, -15, 10}, {2, -10, 10}},
	UpperFloorDb:   -40,
	UpperHoldMs:    30,
	UpperReleaseMs: 100,
	BassFloorDb:    -40,
	BassHoldMs:     20,
	ByVolume: []VolumeEQ{
		{Hz: 71.7, Q: 0.995, Gains: []VolumeGain{
			{45, 8.97}, {55, 6.07}, {65, 3.77}, {75, 0}, {90, -4.54},
		}},
		{Hz: 3320.8, Q: 2.884, Gains: []VolumeGain{
			{55, 5.35}, {65, 3.38}, {75, 0},
		}},
	},
	CeilingDb: 7,
}

// BiscuitVoicing is biscuit's voicing, for the output chain's tests and its
// default chain.
func BiscuitVoicing() *Voicing { return biscuitVoicing }
