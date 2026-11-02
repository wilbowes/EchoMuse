package client

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/discovery"
)

// stubDiscovery replaces the network calls findController makes: up says
// whether the last-known address answers, found is what a browse returns.
func stubDiscovery(t *testing.T, up *bool, found **discovery.ServerInfo) *int {
	t.Helper()
	browses := 0
	origBrowse, origReach := browseOnce, reachable
	browseOnce = func(context.Context) (*discovery.ServerInfo, error) {
		browses++
		if *found == nil {
			return nil, errors.New("nothing answered")
		}
		return *found, nil
	}
	reachable = func(string, time.Duration) bool { return *up }
	t.Cleanup(func() { browseOnce, reachable = origBrowse, origReach })
	return &browses
}

// #1081: a probe that fails mid-outage must not park the device on mDNS. The
// next round tries the last-known address again, and mDNS never has to work.
func TestTheLastKnownControllerIsTriedAgainAfterAFailedRound(t *testing.T) {
	last := &discovery.ServerInfo{Host: "10.0.0.5", Port: 8767, Addr: "10.0.0.5:8767", TLSPort: 8770}
	c := &ControlClient{lastServer: last}
	up := false
	var found *discovery.ServerInfo
	browses := stubDiscovery(t, &up, &found)

	if s, err := c.findController(context.Background()); err == nil || s != nil {
		t.Fatalf("outage round: got %v, %v; want no server and an error", s, err)
	}
	if *browses != 1 {
		t.Fatalf("outage round browsed %d times; want exactly one bounded browse", *browses)
	}

	up = true // the controller is back; mDNS is still broken
	s, err := c.findController(context.Background())
	if err != nil || s != last {
		t.Fatalf("after the outage: got %v, %v; want the last-known controller", s, err)
	}
}

// With nothing cached and nothing answering, a round returns rather than
// browsing until something answers.
func TestARoundWithNothingToFindReturns(t *testing.T) {
	c := &ControlClient{}
	up := false
	var found *discovery.ServerInfo
	stubDiscovery(t, &up, &found)

	done := make(chan error, 1)
	go func() { _, err := c.findController(context.Background()); done <- err }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("want an error from a round that found nothing")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("findController did not return")
	}
}

func TestABrowseFindsTheControllerWhenTheLastKnownOneIsGone(t *testing.T) {
	c := &ControlClient{lastServer: &discovery.ServerInfo{Addr: "10.0.0.5:8767", TLSPort: 8770}}
	up := false
	moved := &discovery.ServerInfo{Host: "10.0.0.9", Port: 8767, Addr: "10.0.0.9:8767", TLSPort: 8770}
	found := moved
	stubDiscovery(t, &up, &found)

	if s, err := c.findController(context.Background()); err != nil || s != moved {
		t.Fatalf("got %v, %v; want the browsed controller", s, err)
	}
}
