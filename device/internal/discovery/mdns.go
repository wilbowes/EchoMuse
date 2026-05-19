package discovery

import (
	"context"
	"fmt"
	"log"
	"net"
	"time"

	"github.com/grandcat/zeroconf"
)

const serviceType = "_emcontroller._tcp"

type ServerInfo struct {
	Host string
	Port int
	Addr string // host:port
}

func FindServer(ctx context.Context) (*ServerInfo, error) {
	backoff := 5 * time.Second
	maxBackoff := 60 * time.Second

	for {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}

		log.Printf("mDNS: browsing for %s.local...", serviceType)
		info, err := browse(ctx)
		if err == nil && info != nil {
			log.Printf("mDNS: found Clara server at %s", info.Addr)
			return info, nil
		}

		log.Printf("mDNS: no server found, retrying in %s", backoff)
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(backoff):
		}

		backoff *= 2
		if backoff > maxBackoff {
			backoff = maxBackoff
		}
	}
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
				Host: host,
				Port: entry.Port,
				Addr: addr,
			}, nil

		case <-browseCtx.Done():
			return nil, fmt.Errorf("browse timeout")
		}
	}
}

func verifyServer(addr string) bool {
	conn, err := net.DialTimeout("tcp", addr, 500*time.Millisecond)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}
