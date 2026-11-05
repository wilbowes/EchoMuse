package discovery

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func writeTempConfig(t *testing.T, raw string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "controller.json")
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func endpoint(host string, port, tlsPort int) *ServerInfo {
	return &ServerInfo{
		Host:    host,
		Port:    port,
		Addr:    net.JoinHostPort(host, strconv.Itoa(port)),
		TLSPort: tlsPort,
	}
}

func addrs(eps []*ServerInfo) []string {
	out := make([]string, len(eps))
	for i, ep := range eps {
		out[i] = ep.Addr
	}
	return out
}

// #106: the configured list is followed by the last controller that actually
// registered, then by mDNS. The list is not authoritative, so a stale entry
// falls through rather than stranding the device on it.
func TestAPassIsTheListThenTheLastKnownThenMDNS(t *testing.T) {
	configured := &StaticConfig{
		Endpoints: []*ServerInfo{
			endpoint("10.20.40.110", 8767, 8770),
			endpoint("echomuse.lan", 8767, 8770),
		},
		MDNS: true,
	}
	lastKnown := endpoint("10.0.0.9", 8767, 8770)

	p := NewPlan(configured, lastKnown)

	want := []string{"10.20.40.110:8767", "echomuse.lan:8767", "10.0.0.9:8767"}
	if got := addrs(p.Endpoints); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("Endpoints = %v, want %v", got, want)
	}
	if !p.MDNS {
		t.Fatal("mDNS must follow the last-known controller unless the config turns it off")
	}
	if p.Len() != 4 {
		t.Fatalf("Len() = %d, want 4 (three endpoints and the mDNS slot)", p.Len())
	}
}

// A pinned test fleet must never browse. The list and the last-known address
// it already registered with are still tried.
func TestMDNSOffStillTriesTheListAndTheLastKnown(t *testing.T) {
	configured := &StaticConfig{
		Endpoints: []*ServerInfo{endpoint("10.20.40.110", 8767, 8770)},
		MDNS:      false,
	}
	lastKnown := endpoint("10.0.0.9", 8767, 8770)

	p := NewPlan(configured, lastKnown)

	if p.MDNS {
		t.Fatal("mdns:false must not plan an mDNS browse")
	}
	want := []string{"10.20.40.110:8767", "10.0.0.9:8767"}
	if got := addrs(p.Endpoints); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("Endpoints = %v, want %v", got, want)
	}
	if p.Len() != len(p.Endpoints) {
		t.Fatalf("Len() = %d, want %d — no mDNS slot", p.Len(), len(p.Endpoints))
	}
}

// A malformed controller.json is reported to the caller, which passes no list;
// the last-known controller and mDNS still get the device online.
func TestAMalformedConfigLeavesTheOtherTiers(t *testing.T) {
	path := writeTempConfig(t, "{ this is not json")
	if _, err := configuredEndpointsFromPath(path); err == nil {
		t.Fatal("a malformed config must be reported, not read as empty")
	}

	lastKnown := endpoint("10.0.0.9", 8767, 8770)
	p := NewPlan(nil, lastKnown)

	if !p.MDNS {
		t.Fatal("mDNS must still be planned when the config file cannot be read")
	}
	if got := addrs(p.Endpoints); fmt.Sprint(got) != "[10.0.0.9:8767]" {
		t.Fatalf("Endpoints = %v, want the last-known controller", got)
	}
}

// Before the first registration the config may be absent entirely: mDNS is
// the only tier, and a pass is still one dial target.
func TestAFreshDevicePlansOneMDNSBrowse(t *testing.T) {
	p := NewPlan(nil, nil)
	if !p.MDNS || len(p.Endpoints) != 0 || p.Len() != 1 {
		t.Fatalf("plan = %#v, want one mDNS browse and nothing else", p)
	}
}

// The last-known endpoint is usually the same address the config already
// lists; trying it twice in a row is slower at exactly the moment the device
// is already struggling to reconnect.
func TestTheLastKnownAddressIsNotListedTwice(t *testing.T) {
	configured := &StaticConfig{
		Endpoints: []*ServerInfo{endpoint("10.20.40.110", 8767, 0)},
		MDNS:      true,
	}
	lastKnown := endpoint("10.20.40.110", 8767, 8770)

	p := NewPlan(configured, lastKnown)

	if len(p.Endpoints) != 1 {
		t.Fatalf("Endpoints = %v, want the configured entry alone", addrs(p.Endpoints))
	}
	if p.Endpoints[0].TLSPort != 8770 {
		t.Fatalf("TLSPort = %d, want the port the device registered on (8770)", p.Endpoints[0].TLSPort)
	}
}

// A drop is a reconnect, not a discovery event: the endpoint that registered
// is retried alone while the grace lasts.
func TestADropSticksToTheControllerThatRegistered(t *testing.T) {
	lastKnown := endpoint("10.0.0.9", 8767, 8770)
	p := NewPlan(nil, lastKnown)
	now := time.Now()

	if got := p.StickTo(lastKnown, now.Add(-time.Minute), now, true); got != lastKnown {
		t.Fatalf("StickTo = %v, want the last-known controller inside the grace", got)
	}
	if got := p.StickTo(lastKnown, now.Add(-StickyGrace), now, true); got != nil {
		t.Fatalf("StickTo = %v, want the cascade after the grace", got)
	}
}

// Removing the pinned endpoint from the config has to take effect on the next
// dial, not after the grace.
func TestAnEndpointRemovedFromTheConfigIsNotSticky(t *testing.T) {
	pinned := endpoint("10.0.0.9", 8767, 8770)
	configured := &StaticConfig{
		Endpoints: []*ServerInfo{endpoint("10.20.40.110", 8767, 8770)},
		MDNS:      true,
	}
	p := NewPlan(configured, nil)
	now := time.Now()

	if got := p.StickTo(pinned, now, now, true); got != nil {
		t.Fatalf("StickTo = %v, want nil for an endpoint no longer in the plan", got)
	}
}

// An endpoint with no tls_port cannot register once a CA is installed: the
// mDNS slot is what supplies the port, so stickiness must not skip it.
func TestAnEndpointWithNoTLSPortIsNotStickyUnderACA(t *testing.T) {
	pinned := endpoint("10.0.0.9", 8767, 0)
	p := NewPlan(nil, pinned)
	now := time.Now()

	if got := p.StickTo(pinned, now, now, true); got != nil {
		t.Fatalf("StickTo = %v, want nil while a CA needs a TLS port", got)
	}
	if got := p.StickTo(pinned, now, now, false); got != pinned {
		t.Fatalf("StickTo = %v, want the pinned endpoint when plain is allowed", got)
	}
}

// The pinned copy is from the last registration and can predate an edit to
// that address; reconnecting must use what the config says now.
func TestStickinessUsesTheConfiguredEntryForThePinnedAddress(t *testing.T) {
	configured := &StaticConfig{
		Endpoints: []*ServerInfo{endpoint("10.0.0.9", 8767, 8770)},
		MDNS:      true,
	}
	p := NewPlan(configured, nil)
	pinned := endpoint("10.0.0.9", 8767, 0)
	now := time.Now()

	got := p.StickTo(pinned, now, now, true)
	if got == nil || got.TLSPort != 8770 {
		t.Fatalf("StickTo = %v, want the configured entry with tls_port 8770", got)
	}
}
