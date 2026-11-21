//go:build server && bench

package main

import (
	"os"

	"github.com/wilbowes/EchoMuse/internal/client"
)

// Bench: the turn stage is on while /data/local/tmp/em-turnbeam exists, and
// the reply taps also score from idle while /data/local/tmp/em-waketaps does
// (both read once at start; restart the server to switch).
func init() {
	if _, err := os.Stat("/data/local/tmp/em-waketaps"); err == nil {
		wakeTapsIdle = true
	}
	client.TVNullBenchOn = func() bool {
		_, err := os.Stat("/data/local/tmp/em-tvnull")
		return err == nil
	}
	client.TurnBeamBenchOn = func() bool {
		_, err := os.Stat("/data/local/tmp/em-turnbeam")
		return err == nil
	}
}
