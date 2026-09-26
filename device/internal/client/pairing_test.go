package client

import (
	"crypto/tls"
	"crypto/x509"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync/atomic"
	"testing"
	"time"
)

func TestDialPlan(t *testing.T) {
	wss := dialAttempt{tls: true}
	wssPair := dialAttempt{tls: true, pairing: true}
	plain := dialAttempt{}
	plainPair := dialAttempt{pairing: true}

	tests := []struct {
		name    string
		hasCA   bool
		tlsPort int
		pairing bool
		want    []dialAttempt
	}{
		{"CA and TLS listener: wss only", true, 8770, false, []dialAttempt{wss}},
		{"CA, no TLS listener: nothing, never a plain downgrade", true, 0, false, nil},
		{"CA, pairing: wss, then plain to ask", true, 8770, true, []dialAttempt{wssPair, plainPair}},
		{"CA, no TLS listener, pairing: plain to ask", true, 0, true, []dialAttempt{plainPair}},
		{"no CA: plain", false, 8770, false, []dialAttempt{plain}},
		{"no CA, no listener: plain", false, 0, false, []dialAttempt{plain}},
		{"no CA, pairing: plain, asking", false, 8770, true, []dialAttempt{plainPair}},
	}
	for _, tt := range tests {
		got := dialPlan(tt.hasCA, tt.tlsPort, tt.pairing)
		if !reflect.DeepEqual(got, tt.want) {
			t.Errorf("%s: dialPlan = %+v, want %+v", tt.name, got, tt.want)
		}
	}
}

func TestTokenNeverRidesPlainWhenACAIsInstalled(t *testing.T) {
	withCA := linkCreds{tlsConf: &tls.Config{}, token: "secret"}
	noCA := linkCreds{token: "secret"}

	tests := []struct {
		name  string
		creds linkCreds
		url   string
		want  string
	}{
		{"CA, wss", withCA, "wss://10.0.0.1:8770", "secret"},
		{"CA, plain", withCA, "ws://10.0.0.1:8767", ""},
		{"no CA, plain", noCA, "ws://10.0.0.1:8767", "secret"},
		{"no token", linkCreds{}, "ws://10.0.0.1:8767", ""},
	}
	for _, tt := range tests {
		if got := tt.creds.headerFor(tt.url).Get("X-EM-Token"); got != tt.want {
			t.Errorf("%s: X-EM-Token = %q, want %q", tt.name, got, tt.want)
		}
	}
}

func TestPairWindow(t *testing.T) {
	t0 := time.Unix(1000, 0)
	p := newPairState()

	if p.active(t0, "a") {
		t.Fatal("active before any hold")
	}
	if !p.open(t0, "a") {
		t.Fatal("first open should start the repeater")
	}
	if p.open(t0, "a") {
		t.Fatal("a second hold while the repeater runs must not start another")
	}
	if !p.active(t0.Add(pairWindow-time.Second), "a") {
		t.Fatal("closed before the window ran out")
	}
	if p.active(t0.Add(pairWindow), "a") {
		t.Fatal("still open at the end of the window")
	}

	// New credentials close it: the approval has been delivered, and the
	// redial it causes must not ask again.
	p.open(t0, "a")
	if p.active(t0.Add(time.Second), "b") {
		t.Fatal("open after the credentials changed")
	}
	if p.active(t0.Add(2*time.Second), "a") {
		t.Fatal("reopened by the old fingerprint")
	}
}

func TestPairRepeaterRestartsAfterItStops(t *testing.T) {
	t0 := time.Unix(1000, 0)
	p := newPairState()
	p.open(t0, "a")
	if !p.keepAsking(t0, "a") {
		t.Fatal("repeater stopped while the window is open")
	}
	if p.keepAsking(t0.Add(pairWindow), "a") {
		t.Fatal("repeater kept going after the window closed")
	}
	if !p.open(t0.Add(pairWindow+time.Second), "a") {
		t.Fatal("a hold after the repeater stopped must start a new one")
	}
}

func TestPairHold(t *testing.T) {
	var fired atomic.Int32
	h := NewPairHold(func() { fired.Add(1) })
	h.hold = 30 * time.Millisecond

	// A tap: forwarded, nothing fires.
	if h.Event(true) || h.Event(false) {
		t.Fatal("a tap was swallowed")
	}
	time.Sleep(60 * time.Millisecond)
	if fired.Load() != 0 {
		t.Fatal("a tap started pairing")
	}

	// A hold: the press is forwarded, pairing starts once, the release is
	// swallowed.
	if h.Event(true) {
		t.Fatal("the press of a hold was swallowed")
	}
	time.Sleep(60 * time.Millisecond)
	if fired.Load() != 1 {
		t.Fatalf("fired %d times, want 1", fired.Load())
	}
	if !h.Event(false) {
		t.Fatal("the release ending a pairing hold was forwarded")
	}

	// The next tap is a tap again.
	if h.Event(true) || h.Event(false) {
		t.Fatal("a tap after a hold was swallowed")
	}
	time.Sleep(60 * time.Millisecond)
	if fired.Load() != 1 {
		t.Fatal("a released press fired later")
	}
}

// "Refused" must mean a controller answered with a certificate our CA did not
// sign — never an unreachable one, which is the ordinary disconnected state.
func TestRefusedByTLS(t *testing.T) {
	srv := httptest.NewTLSServer(http.NotFoundHandler())
	defer srv.Close()
	// A pool without the server's CA: what a device holding another
	// controller's CA sees.
	creds := linkCreds{tlsConf: &tls.Config{
		RootCAs:    x509.NewCertPool(),
		ServerName: "example.com",
	}}
	d := creds.dialer()
	_, _, err := d.Dial("wss://"+srv.Listener.Addr().String()+"/control", nil)
	if err == nil || !refusedByTLS(err) {
		t.Fatalf("a certificate from another CA is a refusal: %v", err)
	}

	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	addr := ln.Addr().String()
	ln.Close()
	_, _, err = d.Dial("wss://"+addr+"/control", nil)
	if err == nil || refusedByTLS(err) {
		t.Fatalf("nothing listening is not a refusal: %v", err)
	}
}
