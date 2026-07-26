package alsa

import (
	"fmt"
	"log"
	"os/exec"
	"strconv"
	"strings"
)

// Control is one mixer setting to apply before opening a PCM.
//
// Name is preferred because control indices shift between kernel builds;
// Index is the fallback for controls whose names are ambiguous. Setting by
// name passes the name as a single argv element, so embedded spaces are fine.
type Control struct {
	Name   string
	Index  int
	Values []string
	// Optional makes a failure to apply non-fatal. Use for controls that only
	// exist on some board revisions.
	Optional bool
}

func (c Control) selector() string {
	if c.Name != "" {
		return c.Name
	}
	return strconv.Itoa(c.Index)
}

// Apply writes a set of mixer controls via tinymix, which ships in
// /system/bin on LineageOS. Controls are applied in order; earlier entries may
// be prerequisites of later ones.
func Apply(card int, controls []Control) error {
	for _, c := range controls {
		args := append([]string{"-D", strconv.Itoa(card), c.selector()}, c.Values...)
		out, err := exec.Command("tinymix", args...).CombinedOutput()
		if err != nil {
			if c.Optional {
				log.Printf("mixer: optional control %q failed: %v (%s)",
					c.selector(), err, strings.TrimSpace(string(out)))
				continue
			}
			return fmt.Errorf("tinymix %s %v: %w (%s)",
				c.selector(), c.Values, err, strings.TrimSpace(string(out)))
		}
	}
	return nil
}
