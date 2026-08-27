package discovery

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
)

// StaticControllerPath is the optional persistent controller endpoint list.
// Useful when a device reaches its controller through a routed tunnel or an
// isolated VLAN where mDNS cannot cross (mDNS is link-local); when absent,
// callers fall back to ordinary mDNS discovery.
const StaticControllerPath = "/data/local/etc/echomuse/controller.json"

type staticEndpoint struct {
	Host    string `json:"host"`
	Port    int    `json:"port"`
	TLSPort int    `json:"tls_port"`
}

type staticControllerConfig struct {
	// Endpoints is tried in order: a static address, a backup address and a
	// DNS name all behave identically here, so callers list them in one
	// ordered array rather than distinguishing them by key. A stale entry
	// falls through to the next one instead of being retried forever.
	Endpoints []staticEndpoint `json:"endpoints"`

	// MDNS controls whether the list is followed by one bounded mDNS
	// attempt once every entry has failed. A *bool so "absent" (→ true)
	// is distinguishable from an explicit false: mDNS is the safety net
	// that keeps a device from being permanently stranded by a stale
	// list, so it defaults ON. false is the deliberate opt-out for a
	// pinned test fleet that must never reach a different controller by
	// accident (device-side only, per #106 — a controller-side switch
	// would also silently kill Home Assistant's mDNS discovery of the
	// fleet's ESPHome/BLE-proxy services, which share the same stack).
	MDNS *bool `json:"mdns"`
}

// StaticConfig is the parsed, validated contents of the optional static
// controller config file.
type StaticConfig struct {
	Endpoints []*ServerInfo
	MDNS      bool
}

// ConfiguredEndpoints reads the optional persistent controller endpoint
// list, in priority order.
//
// Read fresh on every call — callers must call it once per connection
// attempt rather than caching the result, the same contract loadLinkCreds
// follows for /data/local/etc/echomuse/{ca.pem,token}. A device stuck on a
// dead endpoint is exactly the one an operator cannot easily restart, so
// editing the file has to take effect on the very next reconnect.
//
// An absent file is the normal pre-configuration state, not an error, and
// returns (nil, nil). An invalid file is reported rather than silently
// ignored, so a typo doesn't read as "no static config."
func ConfiguredEndpoints() (*StaticConfig, error) {
	return configuredEndpointsFromPath(StaticControllerPath)
}

func configuredEndpointsFromPath(path string) (*StaticConfig, error) {
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}

	var cfg staticControllerConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if len(cfg.Endpoints) == 0 {
		return nil, fmt.Errorf("%s: endpoints must not be empty", path)
	}

	servers := make([]*ServerInfo, 0, len(cfg.Endpoints))
	for i, ep := range cfg.Endpoints {
		host := strings.TrimSpace(ep.Host)
		if host == "" {
			return nil, fmt.Errorf("%s: endpoints[%d]: host is required", path, i)
		}
		if ep.Port < 1 || ep.Port > 65535 {
			return nil, fmt.Errorf("%s: endpoints[%d]: port must be between 1 and 65535", path, i)
		}
		if ep.TLSPort < 0 || ep.TLSPort > 65535 {
			return nil, fmt.Errorf("%s: endpoints[%d]: tls_port must be 0 or between 1 and 65535", path, i)
		}
		// No type field: host is parsed as an IP and resolved as a DNS name
		// if that fails — resolution happens implicitly at dial time, so a
		// static address, a backup address and a DNS name genuinely need no
		// distinguishing here.
		servers = append(servers, &ServerInfo{
			Host:    host,
			Port:    ep.Port,
			Addr:    net.JoinHostPort(host, strconv.Itoa(ep.Port)),
			TLSPort: ep.TLSPort,
		})
	}

	mdns := true
	if cfg.MDNS != nil {
		mdns = *cfg.MDNS
	}
	return &StaticConfig{Endpoints: servers, MDNS: mdns}, nil
}
