//go:build server && bench

package main

import (
	"github.com/wilbowes/EchoMuse/internal/bindings/mic"
	"github.com/wilbowes/EchoMuse/internal/client"
)

// Bench only: the wake scorers' inputs go into the raw capture beside the
// mics (rawcap-*.s, .t0...), so the device's scoring can be replayed exactly.
func init() { client.ScorerTap = mic.RawcapExtra }
