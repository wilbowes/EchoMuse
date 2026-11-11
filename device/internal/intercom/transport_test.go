package intercom

import (
	"errors"
	"net"
	"testing"
	"time"
)

func TestHandshakeRoundTrip(t *testing.T) {
	a, b := net.Pipe()
	defer a.Close()
	defer b.Close()
	want := handshake{Session: "s1", Token: "tok", Caller: "kitchen"}
	done := make(chan error, 1)
	go func() { done <- writeHandshake(a, want) }()
	got, err := readHandshake(b)
	if err != nil {
		t.Fatalf("readHandshake: %v", err)
	}
	if err := <-done; err != nil {
		t.Fatalf("writeHandshake: %v", err)
	}
	if got != want {
		t.Fatalf("handshake = %+v, want %+v", got, want)
	}
}

func TestHandshakeBadMagic(t *testing.T) {
	a, b := net.Pipe()
	defer a.Close()
	defer b.Close()
	go func() { _, _ = a.Write([]byte("XXXX\x01")) }()
	if _, err := readHandshake(b); !errors.Is(err, ErrHandshake) {
		t.Fatalf("readHandshake bad magic: err = %v, want ErrHandshake", err)
	}
}

func TestHandshakeBadVersion(t *testing.T) {
	a, b := net.Pipe()
	defer a.Close()
	defer b.Close()
	go func() { _, _ = a.Write([]byte("EMIC\x09")) }()
	if _, err := readHandshake(b); !errors.Is(err, ErrHandshake) {
		t.Fatalf("readHandshake bad version: err = %v, want ErrHandshake", err)
	}
}

func TestDialAndAccept(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	sess := Session{ID: "s1", Token: "tok"}
	accepted := make(chan error, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			accepted <- err
			return
		}
		defer conn.Close()
		_, err = acceptHandshake(conn, sess, true)
		accepted <- err
	}()
	conn, err := dial(ln.Addr().String(), handshake{Session: "s1", Token: "tok", Caller: "kitchen"})
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	conn.Close()
	if err := <-accepted; err != nil {
		t.Fatalf("acceptHandshake: %v", err)
	}
}

func TestDialBadTokenRejected(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	sess := Session{ID: "s1", Token: "tok"}
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		_, _ = acceptHandshake(conn, sess, true)
	}()
	if _, err := dial(ln.Addr().String(), handshake{Session: "s1", Token: "WRONG", Caller: "kitchen"}); !errors.Is(err, ErrBadToken) {
		t.Fatalf("dial bad token: err = %v, want ErrBadToken", err)
	}
}

func TestDialUnknownSessionRejected(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	sess := Session{ID: "s1", Token: "tok"}
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		_, _ = acceptHandshake(conn, sess, true)
	}()
	if _, err := dial(ln.Addr().String(), handshake{Session: "s2", Token: "tok", Caller: "kitchen"}); !errors.Is(err, ErrUnknownSession) {
		t.Fatalf("dial unknown session: err = %v, want ErrUnknownSession", err)
	}
}

func TestDialBusyRejected(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	sess := Session{ID: "s1", Token: "tok"}
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		_, _ = acceptHandshake(conn, sess, false) // not waiting for a call
	}()
	if _, err := dial(ln.Addr().String(), handshake{Session: "s1", Token: "tok", Caller: "kitchen"}); !errors.Is(err, ErrBusy) {
		t.Fatalf("dial busy: err = %v, want ErrBusy", err)
	}
}

func TestDialTimeout(t *testing.T) {
	// Nothing listens here; dial must fail within the handshake
	// timeout rather than hanging. Use a black-holed address family
	// the test host refuses fast, and bound the whole test.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := ln.Addr().String()
	ln.Close() // now nothing listens on addr
	start := time.Now()
	if _, err := dial(addr, handshake{Session: "s1", Token: "tok"}); err == nil {
		t.Fatal("dial closed port: want an error")
	}
	if time.Since(start) > HandshakeTimeout+5*time.Second {
		t.Fatalf("dial took %v, want it bounded", time.Since(start))
	}
}
