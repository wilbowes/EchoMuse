//go:build server && bench

package main

import (
	"os"

	"github.com/wilbowes/EchoMuse/internal/client"
)

// Bench: the turn stage is on while /data/local/tmp/em-turnbeam exists
// (read once at start; restart the server to switch).
func init() {
	client.TurnBeamBenchOn = func() bool {
		_, err := os.Stat("/data/local/tmp/em-turnbeam")
		return err == nil
	}
	// The second look on wake near-misses: while em-secondlook exists.
	SecondLookBenchOn = func() bool {
		_, err := os.Stat("/data/local/tmp/em-secondlook")
		return err == nil
	}
}
