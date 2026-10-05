package client

import (
	"net"
	"syscall"
	"testing"

	"github.com/gorilla/websocket"
)

// The option has to reach the kernel on the socket the link actually uses, so
// read it back from a real connection made by the real dialer rather than
// trusting the Control hook ran.
func TestDialerSetsThinStreamOnTheSocket(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		if c, err := ln.Accept(); err == nil {
			defer c.Close()
			buf := make([]byte, 1)
			c.Read(buf)
		}
	}()

	conn, err := netDialer().Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	raw, err := conn.(*net.TCPConn).SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	var got int
	var gerr error
	raw.Control(func(fd uintptr) {
		got, gerr = syscall.GetsockoptInt(int(fd), syscall.IPPROTO_TCP, tcpThinLinearTimeouts)
	})
	if gerr != nil {
		t.Fatalf("getsockopt: %v", gerr)
	}
	if got != 1 {
		t.Fatalf("TCP_THIN_LINEAR_TIMEOUTS = %d, want 1", got)
	}
}

// Every plane dials through creds.dialer(), so that is where the hook must be.
func TestLinkDialerUsesTheTunedNetDialer(t *testing.T) {
	if (linkCreds{}).dialer().NetDialContext == nil {
		t.Fatal("device-link dialer bypasses the thin-stream net dialer")
	}
}

func TestLinkLossNeedsABaselineAndSurvivesReconnects(t *testing.T) {
	a, b := &websocket.Conn{}, &websocket.Conn{}
	var l LinkLoss
	if r, s := l.Drain(TCPSnap{a, 1000, 40}); r != nil || s != nil {
		t.Fatal("a first sighting is a baseline, not a burst")
	}
	r, s := l.Drain(TCPSnap{a, 1500, 45})
	if r == nil || *r != 5 || s == nil || *s != 500 {
		t.Fatalf("got retrans=%v segs=%v", r, s)
	}
	// b is a reconnect: baseline only, a carries on.
	r, _ = l.Drain(TCPSnap{a, 1600, 46}, TCPSnap{b, 10, 0})
	if r == nil || *r != 1 {
		t.Fatalf("reconnect leaked into the delta: %v", r)
	}
}

// FireOS 5's kernel does not fill tcpi_segs_out: report the count, never a
// fabricated zero-segment rate.
func TestLinkLossWithoutSegmentsIsACountOnly(t *testing.T) {
	a := &websocket.Conn{}
	var l LinkLoss
	l.Drain(TCPSnap{a, 0, 7})
	r, s := l.Drain(TCPSnap{a, 0, 9})
	if r == nil || *r != 2 || s != nil {
		t.Fatalf("got retrans=%v segs=%v", r, s)
	}
}

func TestLinkLossWithNothingMeasuredIsNil(t *testing.T) {
	var l LinkLoss
	if r, s := l.Drain(); r != nil || s != nil {
		t.Fatal("nothing measured must stay nil (NULL), not a clean link")
	}
}
