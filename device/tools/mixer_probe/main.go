//go:build server

// mixer_probe — read mixer controls by name through internal/bindings/mixer,
// the same path the firmware uses, so its answers can be checked against
// `tinymix -D 0 <name>` on a device. Read-only.
//
//	mixer_probe "PCM Playback Volume" "ADC_A Left Mute" ...
//
// Build: device/tools/build_tools.sh (module tool, -tags server).
package main

import (
	"fmt"
	"os"

	"github.com/wilbowes/EchoMuse/internal/bindings/mixer"
)

func main() {
	bad := 0
	for _, name := range os.Args[1:] {
		v, err := mixer.Get(name)
		if err != nil {
			fmt.Printf("%s: ERROR %v\n", name, err)
			bad++
			continue
		}
		fmt.Printf("%s: %s\n", name, v)
	}
	if bad > 0 {
		os.Exit(1)
	}
}
