package discovery

import (
	"context"
	"fmt"
	"log"
	"net"
	"time"

	"github.com/hashicorp/mdns"
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
	entries := make(chan *mdns.ServiceEntry, 8)
	timeout := 10 * time.Second

	browseCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	iface, err := net.InterfaceByName("wlan0")
	if err != nil {
		log.Printf("mDNS: could not find wlan0, using default interface: %v", err)
		iface = nil
	}

	go func() {
		defer close(entries)
		params := &mdns.QueryParam{
			Service:             serviceType,
			Domain:              "local",
			Timeout:             timeout,
			Entries:             entries,
			WantUnicastResponse: false,
			Interface:           iface,
		}
		if err := mdns.Query(params); err != nil {
			log.Printf("mDNS query error: %v", err)
		}
	}()

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
			if len(entry.AddrV4) > 0 {
				host = entry.AddrV4.String()
			} else if len(entry.Addr) > 0 {
				host = entry.Addr.String()
			}

			if host == "" {
				addrs, err := net.LookupHost(entry.Host)
				if err == nil && len(addrs) > 0 {
					host = addrs[0]
				}
			}

			if host == "" {
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
