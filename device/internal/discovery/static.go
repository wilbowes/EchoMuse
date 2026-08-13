package discovery

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
)

const StaticControllerPath = "/data/local/etc/echomuse/controller.json"

type staticControllerConfig struct {
	Host    string `json:"host"`
	Port    int    `json:"port"`
	TLSPort int    `json:"tls_port"`
}

// ConfiguredServer reads the optional persistent controller endpoint.
//
// A static endpoint is useful when the device reaches its controller through
// a routed tunnel: mDNS is link-local and cannot discover a controller across
// that boundary. When the file is absent, callers retain the normal mDNS
// discovery path. An invalid file is reported rather than silently ignored.
func ConfiguredServer() (*ServerInfo, error) {
	return configuredServerFromPath(StaticControllerPath)
}

func configuredServerFromPath(path string) (*ServerInfo, error) {
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

	cfg.Host = strings.TrimSpace(cfg.Host)
	if cfg.Host == "" {
		return nil, fmt.Errorf("%s: host is required", path)
	}
	if cfg.Port < 1 || cfg.Port > 65535 {
		return nil, fmt.Errorf("%s: port must be between 1 and 65535", path)
	}
	if cfg.TLSPort < 0 || cfg.TLSPort > 65535 {
		return nil, fmt.Errorf("%s: tls_port must be 0 or between 1 and 65535", path)
	}

	return &ServerInfo{
		Host:    cfg.Host,
		Port:    cfg.Port,
		Addr:    net.JoinHostPort(cfg.Host, strconv.Itoa(cfg.Port)),
		TLSPort: cfg.TLSPort,
	}, nil
}
