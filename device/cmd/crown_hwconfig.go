//go:build crown

package main

// crown: two ADCs (A/B, one per die) — docs/echo-show-8-hardware-map.md.
// Named, not indexed: biscuit's numeric indices address unrelated controls
// here (confirmed live 2026-08-26 — ctl 89/107/125/143 all failed with
// "invalid value"/"control only takes 1", not a crown-analogue of gain).
var adcDigitalGainCtls = []string{"ADC_A Digital Volume Control", "ADC_B Digital Volume Control"}
var adcMicpgaCtls = []string{"ADC_A MICPGA Volume Ctrl", "ADC_B MICPGA Volume Ctrl"}
