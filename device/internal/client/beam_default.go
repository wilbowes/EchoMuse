//go:build !crown

package client

import "github.com/wilbowes/EchoMuse/internal/beamformer"

// newBeam constructs biscuit's real beamformer. Tagged "not crown" rather
// than "server" for the same reason as internal/bindings/led and
// capabilities_default.go: this package is host-tested with no board tag at
// all, and biscuit is the default for any build that isn't explicitly
// another board.
func newBeam() beamEngine { return beamformer.New() }
