package client

import (
	"log"
	"net"
	"sync"
	"syscall"
	"time"
)

// Thin-stream TCP for the device link (all three planes).
//
// The link loses packets — 4.6-7.1% measured in #139, far more while the BLE
// scan runs — and TCP turns each loss into a wait: one lost segment holds
// everything behind it until the retransmission timer fires, and a second
// loss doubles the timer. On an RTO already driven to ~1s by the loss, three
// losses in a row are a 7s stall, which is how a 6ms link logs multi-second
// "RTT" and how a keepalive times out.
//
// Linux's thin-stream option is made for exactly this traffic: a connection
// with fewer than four segments in flight — the control plane almost always,
// the data plane between replies — retransmits on a LINEAR timer for its
// first six retries instead of doubling. Bulk transfers are unaffected, since
// they are not thin. The controller's host may already set it system-wide
// (HA OS does); the Echo's kernel does not (tcp_thin_linear_timeouts=0), and
// the retransmits that matter from this end are the pongs, events and the
// user's words going up.
//
// TCP_THIN_DUPACK (fast retransmit on the first dup ACK) is set alongside
// it: both device kernels have it; kernels from 4.10 on accept and ignore it.
const (
	tcpThinLinearTimeouts = 16 // include/uapi/linux/tcp.h
	tcpThinDupack         = 17
)

var tuneWarnOnce sync.Once

// tuneTCP is a net.Dialer Control hook. A failure is logged once and never
// fails the dial: an untuned link is the old behaviour, not a broken one.
func tuneTCP(network, address string, c syscall.RawConn) error {
	var serr error
	if err := c.Control(func(fd uintptr) {
		for _, opt := range []int{tcpThinLinearTimeouts, tcpThinDupack} {
			if e := syscall.SetsockoptInt(int(fd), syscall.IPPROTO_TCP, opt, 1); e != nil && serr == nil {
				serr = e
			}
		}
	}); err != nil {
		serr = err
	}
	if serr != nil {
		tuneWarnOnce.Do(func() {
			log.Printf("[link] thin-stream TCP not applied (%v) — link keeps exponential backoff", serr)
		})
	}
	return nil
}

// netDialer is what every device-link dial goes through.
func netDialer() *net.Dialer {
	return &net.Dialer{Timeout: 10 * time.Second, Control: tuneTCP}
}
