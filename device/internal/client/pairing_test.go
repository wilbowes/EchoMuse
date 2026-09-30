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
	wait := func() { time.Sleep(60 * time.Millisecond) }

	// The action button alone, however long: Home Assistant's long press,
	// never pairing.
	if h.Action(true) {
		t.Fatal("an action press was swallowed")
	}
	wait()
	if h.Action(false) || fired.Load() != 0 {
		t.Fatal("the action button alone started pairing")
	}

	// Volume-up alone: a volume step, never pairing.
	h.VolumeUp(true)
	wait()
	if h.VolumeUp(false) || fired.Load() != 0 {
		t.Fatal("volume-up alone started pairing")
	}

	// Both, released early: nothing fires and both releases pass through.
	h.Action(true)
	h.VolumeUp(true)
	if h.VolumeUp(false) || h.Action(false) {
		t.Fatal("a short combination swallowed a release")
	}
	wait()
	if fired.Load() != 0 {
		t.Fatal("a released combination fired later")
	}

	// Both held, in either order: fires once, and both releases are
	// swallowed so it is neither a long press nor a volume step.
	h.VolumeUp(true)
	if h.Action(true) {
		t.Fatal("the press completing the combination was swallowed")
	}
	wait()
	if fired.Load() != 1 {
		t.Fatalf("fired %d times, want 1", fired.Load())
	}
	if !h.Action(false) {
		t.Fatal("the action release ending a pairing hold was forwarded")
	}
	if !h.VolumeUp(false) {
		t.Fatal("the volume-up release ending a pairing hold stepped the volume")
	}

	// Afterwards each button is itself again.
	if h.Action(true) || h.Action(false) || h.VolumeUp(true) || h.VolumeUp(false) {
		t.Fatal("a press after a pairing hold was swallowed")
	}
	wait()
	if fired.Load() != 1 {
		t.Fatal("pairing fired again")
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
