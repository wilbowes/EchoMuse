//go:build !crown

package server

// volumeSelector/volumeDisplayName: biscuit's DAC digital volume (tlv320aic32x4
// ctl 61, "PCM Playback Volume"). Tagged "not crown" rather than "server" —
// same reason as internal/bindings/led and capabilities_default.go: this
// package is host-tested with no board tag at all, and biscuit stays the
// default for any build that isn't explicitly another board.
const volumeSelector = "61"
const volumeDisplayName = "PCM Playback Volume"
