package bluetooth

import (
	"log"
	"os/exec"

	"github.com/wilbowes/EchoMuse/internal/platform"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// stopHCIDaemons stops the vendor's Bluetooth daemons the board states
// (Hardware.HCIDaemons) before each open: the HCI node has one owner, and
// while any of them runs, or crash-loops reopening it every few seconds, the
// scanner cannot use the chip. FireOS only: they are Android init services,
// and emOS starts none of them. `stop` on a service that is not running or
// not defined is harmless, so its result is ignored.
func stopHCIDaemons() {
	names := hciDaemonsToStop(platform.Base(), board.CurrentLayout().HCIDaemons)
	for _, n := range names {
		// The name rides as $1, never inside the command line.
		_ = exec.Command("/system/bin/sh", "-c", `stop "$1"`, "sh", n).Run()
	}
	if len(names) > 0 {
		log.Printf("[ble] stopped %v, which hold the HCI node", names)
	}
}

// hciDaemonsToStop is the decision: which of the board's stated daemons to
// stop on this base OS. Nothing off FireOS, and nothing a shell could read
// as more than a name.
func hciDaemonsToStop(base string, stated []string) []string {
	if base != platform.FireOS {
		return nil
	}
	var names []string
	for _, n := range stated {
		if safeServiceName(n) {
			names = append(names, n)
		}
	}
	return names
}

// safeServiceName keeps a name fit for a shell command line.
func safeServiceName(n string) bool {
	if n == "" {
		return false
	}
	for _, r := range n {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9',
			r == '_', r == '-', r == '.':
		default:
			return false
		}
	}
	return true
}
