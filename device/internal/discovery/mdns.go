package discovery

import (
	"context"
	"fmt"
	"log"
	"net"
	"strconv"
	"strings"
	"time"

	"github.com/grandcat/zeroconf"
)

const serviceType = "_emcontroller._tcp"

type ServerInfo struct {
	Host string
	Port int
	Addr string // host:port
	// TLSPort is the controller's device-link wss listener, from the
	// tls_port TXT property. 0 = controller does not offer TLS (pre-TLS
	// controller or listener disabled).
	TLSPort int
}

// FindServerOnce is a single browse round with no retry loop. The caller
// retries, so it can try other addresses between browses (#1081).
func FindServerOnce(ctx context.Context) (*ServerInfo, error) {
	return browse(ctx)
}

func browse(ctx context.Context) (*ServerInfo, error) {
	entries := make(chan *zeroconf.ServiceEntry, 4)
	timeout := 10 * time.Second
	browseCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	opts := []zeroconf.ClientOption{}
	iface, err := net.InterfaceByName("wlan0")
	if err != nil {
		log.Printf("mDNS: could not find wlan0, using default interface: %v", err)
	} else {
		opts = append(opts, zeroconf.SelectIfaces([]net.Interface{*iface}))
	}

	resolver, err := zeroconf.NewResolver(opts...)
	if err != nil {
		return nil, fmt.Errorf("mDNS resolver error: %v", err)
	}

	if err := resolver.Browse(browseCtx, serviceType, "local.", entries); err != nil {
		return nil, fmt.Errorf("mDNS browse error: %v", err)
	}

	for {
		select {
		case entry, ok := <-entries:
			if !ok {
				return nil, fmt.Errorf("no entries found")
			}
			if entry == nil {
				continue
			}

			host := ""
			if len(entry.AddrIPv4) > 0 {
				host = entry.AddrIPv4[0].String()
			} else if len(entry.AddrIPv6) > 0 {
				host = entry.AddrIPv6[0].String()
			}

			if host == "" {
				log.Printf("mDNS: skipping entry %s — no address (Host=%s)", entry.Instance, entry.HostName)
				continue
			}

			addr := fmt.Sprintf("%s:%d", host, entry.Port)
			if !verifyServer(addr) {
				log.Printf("mDNS: candidate %s failed verification — skipping", addr)
				continue
			}

			return &ServerInfo{
				Host:    host,
				Port:    entry.Port,
				Addr:    addr,
				TLSPort: parseTLSPort(entry.Text),
			}, nil

		case <-browseCtx.Done():
			return nil, fmt.Errorf("browse timeout")
		}
	}
}

// parseTLSPort extracts the tls_port key from mDNS TXT records
// ("key=value" strings). Returns 0 when absent or malformed.
func parseTLSPort(txt []string) int {
	for _, t := range txt {
		if v, ok := strings.CutPrefix(t, "tls_port="); ok {
			if p, err := strconv.Atoi(v); err == nil && p > 0 && p < 65536 {
				return p
			}
		}
	}
	return 0
}

func verifyServer(addr string) bool {
	conn, err := net.DialTimeout("tcp", addr, 500*time.Millisecond)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}
