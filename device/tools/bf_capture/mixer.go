//go:build server

package main

import (
	"log"
	"os/exec"
)

func stopMixer() {
	cmd := exec.Command("stop", "mixer")
	if err := cmd.Run(); err != nil {
		log.Printf("stop mixer: %v (continuing)", err)
	}
}
