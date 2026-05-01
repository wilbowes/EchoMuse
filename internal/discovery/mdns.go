package discovery

import (
	"context"
	"fmt"
	"log"
	"net"
	"time"

	"github.com/hashicorp/mdns"
)

const serviceType = "_clara._tcp"

type ServerInfo struct {
	Host string
	Port int
	Addr string // host:port
}

// FindServer browses mDNS for _clara._tcp.local and returns the first result.
// Retries indefinitely with backoff until a server is found.
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
	timeout := 5 * time.Second

	browseCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	go func() {
		defer close(entries)
		params := &mdns.QueryParam{
			Service:             serviceType,
			Domain:              "local",
			Timeout:             timeout,
			Entries:             entries,
			WantUnicastResponse: false,
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
				// Try resolving the hostname
				addrs, err := net.LookupHost(entry.Host)
				if err == nil && len(addrs) > 0 {
					host = addrs[0]
				}
			}

			if host == "" {
				continue
			}

			return &ServerInfo{
				Host: host,
				Port: entry.Port,
				Addr: fmt.Sprintf("%s:%d", host, entry.Port),
			}, nil

		case <-browseCtx.Done():
			return nil, fmt.Errorf("browse timeout")
		}
	}
}
