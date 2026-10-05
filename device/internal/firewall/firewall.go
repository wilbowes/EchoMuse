// Package firewall opens an inbound TCP port for as long as a listener needs
// it.
//
// emOS's init installs an outbound-only filter (emos/init/init.c firewall():
// INPUT policy DROP), so a listener on the Echo is unreachable until a rule
// admits it. The Sendspin player is the first listener: Music Assistant
// connects IN to it, and without a rule every connection times out while the
// player logs that it is listening. The port is opened only while the
// listener runs, so an Echo with nothing enabled stays outbound-only.
//
// FireOS has no filter and needs no rule; adding one there is harmless.
// ip6tables is absent on emOS built beside FireOS 6's system, and a missing
// binary is not an error: init switches IPv6 off there instead (#702).
package firewall

import (
	"errors"
	"os/exec"
	"strconv"
)

// run executes one filter command. A variable so tests can record calls.
var run = func(bin string, args ...string) error {
	return exec.Command(bin, args...).Run()
}

var tools = []string{"iptables", "ip6tables"}

func rule(port int) []string {
	return []string{"INPUT", "-p", "tcp", "--dport", strconv.Itoa(port), "-j", "ACCEPT"}
}

// Open admits inbound TCP to port. Idempotent: it checks before inserting,
// so a firmware restart does not stack duplicates.
func Open(port int) error {
	var errs []error
	for _, t := range tools {
		if run(t, append([]string{"-C"}, rule(port)...)...) == nil {
			continue
		}
		if err := run(t, append([]string{"-I"}, rule(port)...)...); err != nil && !missing(err) {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// Close removes every copy of the rule, including one added by hand.
// Closing a port that is not open does nothing.
func Close(port int) {
	for _, t := range tools {
		for i := 0; i < 8 && run(t, append([]string{"-D"}, rule(port)...)...) == nil; i++ {
		}
	}
}

func missing(err error) bool { return errors.Is(err, exec.ErrNotFound) }
