package client

import (
	"net"
	"syscall"
	"testing"
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
