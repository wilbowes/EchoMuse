package client

import (
	"crypto/tls"
	"log"
	"net"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"golang.org/x/sys/unix"
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

// ─── Uplink loss from the kernel's own counters ──────────────────────────────
//
// The mirror of the controller's em_tcp.LossWindow: how many segments this end
// had to send twice since the last stats report, over the control and data
// planes. That is uplink loss — the user's words, pongs and events. Rides the
// existing ~30s stats message; one getsockopt per plane.
//
// FireOS 5's kernel (3.18) predates tcpi_segs_out (4.2), so there it is a
// count, not a rate; segments are sent only when the kernel filled them in.

// TCPSnap is one connection's cumulative counters. Key identifies the
// connection, since the counters start again on every reconnect.
type TCPSnap struct {
	Key     *websocket.Conn
	SegsOut uint32
	Retrans uint32
}

// tcpSnap reads the counters off a WebSocket's socket, through TLS if wss.
func tcpSnap(ws *websocket.Conn) (TCPSnap, bool) {
	if ws == nil {
		return TCPSnap{}, false
	}
	nc := ws.UnderlyingConn()
	if t, ok := nc.(*tls.Conn); ok {
		nc = t.NetConn()
	}
	tc, ok := nc.(*net.TCPConn)
	if !ok {
		return TCPSnap{}, false
	}
	raw, err := tc.SyscallConn()
	if err != nil {
		return TCPSnap{}, false
	}
	var info *unix.TCPInfo
	var gerr error
	if err := raw.Control(func(fd uintptr) {
		info, gerr = unix.GetsockoptTCPInfo(int(fd), unix.IPPROTO_TCP, unix.TCP_INFO)
	}); err != nil || gerr != nil || info == nil {
		return TCPSnap{}, false
	}
	return TCPSnap{
		Key:     ws,
		SegsOut: info.Segs_out,
		Retrans: info.Total_retrans,
	}, true
}

// LinkLoss turns cumulative per-connection counters into per-report deltas.
// A connection seen for the first time is a baseline, and counters that went
// backwards are a new connection: a reconnect must not read as a burst.
type LinkLoss struct {
	last map[*websocket.Conn]TCPSnap
}

// Drain returns retransmits (and segments, where counted) since the last
// call. nil means nothing could be measured, which the controller stores as
// NULL rather than as a clean link.
func (l *LinkLoss) Drain(snaps ...TCPSnap) (retrans, segs *uint64) {
	now := make(map[*websocket.Conn]TCPSnap, len(snaps))
	var r, s uint64
	measured, segsKnown := false, false
	for _, sn := range snaps {
		now[sn.Key] = sn
		prev, ok := l.last[sn.Key]
		if !ok || sn.Retrans < prev.Retrans || sn.SegsOut < prev.SegsOut {
			continue
		}
		r += uint64(sn.Retrans - prev.Retrans)
		s += uint64(sn.SegsOut - prev.SegsOut)
		measured = true
		if sn.SegsOut > 0 {
			segsKnown = true
		}
	}
	l.last = now
	if !measured {
		return nil, nil
	}
	if !segsKnown {
		return &r, nil
	}
	return &r, &s
}

// TCPSnapshot reads the control connection's counters.
func (c *ControlClient) TCPSnapshot() (TCPSnap, bool) {
	c.connMu.Lock()
	defer c.connMu.Unlock()
	return tcpSnap(c.conn)
}

// TCPSnapshot reads the data connection's counters.
func (d *DataClient) TCPSnapshot() (TCPSnap, bool) {
	d.connMu.Lock()
	defer d.connMu.Unlock()
	return tcpSnap(d.conn)
}
