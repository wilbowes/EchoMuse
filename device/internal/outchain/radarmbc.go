package outchain

// Radar's full 4-band MBCL, configured from the Echo's own MBCL.cfg
// (RadarTuning.MBCL). This is the bit-exact Go mirror of
// controller/em_mbc.py's RadarMultiband — see that class's docstring for the
// derivation: why a naive recursive crossover split does NOT sum flat
// (1.59dB of measured ripple) and the allpass-compensation fix that makes it
// exact (4.6e-11dB), and the one number that is inferred rather than read
// (each band's compressor reuses its own limiter's release — the config has
// no comp_release field at all).

// radarMultiband is Radar's full MBCL — three crossovers splitting the
// signal into four bands, each running its own compressor then its own
// limiter, both with their own input trim; the four are summed. The
// combined full-band limiter (radarLimiterThresholdDb/ReleaseMs in
// limiter.go) is NOT part of this type — it is MBCL's own "Full-band
// limiter" entry and stays exactly where it already runs, downstream of
// this, in chain.go.
//
// EXACT FLATNESS, THROUGH ALLPASS COMPENSATION — see RadarMultiband's
// Python docstring for the derivation. Band 1 (which only ever sees the
// fc1 split) is additionally run through an allpass of fc2 and then of
// fc3; band 2 (fc1 and fc2) is additionally run through an allpass of
// fc3; bands 3 and 4 already carry all three splits' worth of filtering.
// Every *c/*c1/*c2 filter below is an INDEPENDENT instance of the same
// coefficients as the real split it stands in for — same transfer
// function, separate state, because it filters a different signal.
type radarMultiband struct {
	enabled bool
	sysGain float64 // MBCL.cfg's inVol, on the whole signal ahead of the split

	lp1, hp1     [2]biquad // fc1 split, on x
	lp2, hp2     [2]biquad // fc2 split, on high1
	lp2c, hp2c   [2]biquad // fc2 compensation, on low1
	lp3, hp3     [2]biquad // fc3 split, on high2 -> band3/4
	lp3c1, hp3c1 [2]biquad // fc3 compensation, on low2 -> band2
	lp3c2, hp3c2 [2]biquad // fc3 compensation, on ap2(low1) -> band1

	comp [4]*stockComp
	lim  [4]*stockLimiter
}

func newRadarMultiband(fs float64, spec *mbclSpec) *radarMultiband {
	lo1, hi1 := butter2(spec.FC[0], fs, false), butter2(spec.FC[0], fs, true)
	lo2, hi2 := butter2(spec.FC[1], fs, false), butter2(spec.FC[1], fs, true)
	lo3, hi3 := butter2(spec.FC[2], fs, false), butter2(spec.FC[2], fs, true)

	m := &radarMultiband{
		sysGain: dbToGain(spec.InVol),
		lp1:     [2]biquad{lo1, lo1}, hp1: [2]biquad{hi1, hi1},
		lp2: [2]biquad{lo2, lo2}, hp2: [2]biquad{hi2, hi2},
		lp2c: [2]biquad{lo2, lo2}, hp2c: [2]biquad{hi2, hi2},
		lp3: [2]biquad{lo3, lo3}, hp3: [2]biquad{hi3, hi3},
		lp3c1: [2]biquad{lo3, lo3}, hp3c1: [2]biquad{hi3, hi3},
		lp3c2: [2]biquad{lo3, lo3}, hp3c2: [2]biquad{hi3, hi3},
	}
	for i, b := range spec.Bands {
		m.comp[i] = newStockComp(fs, b.CompRatio, b.CompThresh, b.CompGainMin, b.CompInVol)
		m.lim[i] = newStockLimiter(fs, b.LimThresh, b.LimRelease, b.LimInVol)
	}
	return m
}

func (m *radarMultiband) step(x float64) float64 {
	low1 := m.lp1[1].step(m.lp1[0].step(x))
	high1 := m.hp1[1].step(m.hp1[0].step(x))

	low2 := m.lp2[1].step(m.lp2[0].step(high1))
	high2 := m.hp2[1].step(m.hp2[0].step(high1))

	ap2Low1 := m.lp2c[1].step(m.lp2c[0].step(low1)) + m.hp2c[1].step(m.hp2c[0].step(low1))

	band3Raw := m.lp3[1].step(m.lp3[0].step(high2))
	band4Raw := m.hp3[1].step(m.hp3[0].step(high2))

	band2Raw := m.lp3c1[1].step(m.lp3c1[0].step(low2)) + m.hp3c1[1].step(m.hp3c1[0].step(low2))
	band1Raw := m.lp3c2[1].step(m.lp3c2[0].step(ap2Low1)) + m.hp3c2[1].step(m.hp3c2[0].step(ap2Low1))

	raw := [4]float64{band1Raw, band2Raw, band3Raw, band4Raw}

	// mbcl_inVol and every band's comp_inVol/lim_inVol apply EITHER WAY —
	// fixed gain stages, not dynamics ones. Gating them on `enabled` would
	// make the toggle step the level by as much as 6dB (band 3's
	// comp_inVol+lim_inVol) on top of whatever the law itself was doing.
	// Only levelGain() — the compression/limiting itself — is skipped
	// while disabled, which also freezes its gain state, same as
	// bassGuard's own bypass.
	var out float64
	for i := 0; i < 4; i++ {
		y := m.comp[i].step(raw[i] * m.sysGain) // comp_inVol inside
		y = m.lim[i].step(y)                    // lim_inVol inside
		out += y
	}
	return out
}

func (m *radarMultiband) reset() {
	for i := range m.lp1 {
		m.lp1[i].reset()
		m.hp1[i].reset()
	}
	for i := range m.lp2 {
		m.lp2[i].reset()
		m.hp2[i].reset()
	}
	for i := range m.lp2c {
		m.lp2c[i].reset()
		m.hp2c[i].reset()
	}
	for i := range m.lp3 {
		m.lp3[i].reset()
		m.hp3[i].reset()
	}
	for i := range m.lp3c1 {
		m.lp3c1[i].reset()
		m.hp3c1[i].reset()
	}
	for i := range m.lp3c2 {
		m.lp3c2[i].reset()
		m.hp3c2[i].reset()
	}
	for i := range m.comp {
		m.comp[i].reset()
		m.lim[i].reset()
	}
}

// setEnabled/setFloorDb/takeMaxReductionDb complete bassStage — see
// chain.go.
func (m *radarMultiband) setEnabled(enabled bool) {
	m.enabled = enabled
	for i := range m.comp {
		m.comp[i].enabled = enabled
		m.lim[i].setEnabled(enabled)
	}
}

// setFloorDb reaches band 1's floor only — the one dashboard control
// Radar's guard has (bassGuardDb), same meaning as before this class
// existed. Bands 2-4 have no control, same reasoning as the limiter
// override: there is nothing today to leave untouched.
func (m *radarMultiband) setFloorDb(floorDb float64) { m.comp[0].setFloorDb(floorDb) }

func (m *radarMultiband) takeMaxReductionDb() float64 {
	var worst float64
	for i := 0; i < 4; i++ {
		if r := m.comp[i].takeMaxReductionDb(); r > worst {
			worst = r
		}
		if r, _, _ := m.lim[i].takeStats(); r > worst {
			worst = r
		}
	}
	return worst
}
