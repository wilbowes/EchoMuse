//go:build crown

package server

// volumeSelector/volumeDisplayName: crown's RT5616 DAC1 digital volume,
// "DAC1 Playback Volume" (docs/echo-show-8-hardware-map.md — same control
// name checkers uses, shared codec). Confirmed live 2026-08-26:
// `tinymix -D 0 'DAC1 Playback Volume'` -> "DAC1 Playback Volume: 173 173
// (dsrange 0->175)" — same "<name>: L R (...)" shape volume.go's Sscanf
// expects, name-selected rather than by index because biscuit's index 61
// addresses an unrelated control here (Pmic_Anc_Switch) — the exact trap
// CLAUDE.md warns about for input-device numbering, same failure mode on
// a mixer control.
//
// volumeMax (server/volume.go) stays at biscuit's THD-calibrated 127 cap
// for now. That calibration was measured against biscuit's TLV320AIC32x4,
// not crown's RT5616 — unmeasured here, so 127 is a conservative carry-over
// (same 0-175 native range, same codec family as checkers) rather than a
// verified safe ceiling. Needs its own THD sweep before being trusted the
// way biscuit's is.
const volumeSelector = "DAC1 Playback Volume"
const volumeDisplayName = "DAC1 Playback Volume"
