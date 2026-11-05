package discovery

import "time"

// StickyGrace is how long a dropped link keeps reconnecting to the controller
// that last registered before the resolution cascade starts again from the
// top. A controller restart, an add-on update or a bad link all come back at
// the same address, so rediscovering for them means a multicast browse on the
// link that is already the problem.
const StickyGrace = 2 * time.Minute

// Plan is one pass of the resolution order from #106: the configured
// endpoints in order, then the controller that last registered, then one
// bounded mDNS browse unless the config turns it off. A configured list is
// not authoritative — a stale entry falls through to the next tier.
type Plan struct {
	Endpoints []*ServerInfo
	MDNS      bool
}

// NewPlan builds a pass. lastKnown is the endpoint persisted by the last
// successful registration, nil before the first one.
func NewPlan(configured *StaticConfig, lastKnown *ServerInfo) Plan {
	p := Plan{MDNS: configured == nil || configured.MDNS}
	if configured != nil {
		p.Endpoints = append(p.Endpoints, configured.Endpoints...)
	}
	if lastKnown == nil {
		return p
	}
	for _, ep := range p.Endpoints {
		if ep.Addr == lastKnown.Addr {
			// The configured entry for this address has no tls_port, yet
			// the device registered there over TLS: keep the port that
			// worked.
			if ep.TLSPort == 0 {
				ep.TLSPort = lastKnown.TLSPort
			}
			return p
		}
	}
	p.Endpoints = append(p.Endpoints, lastKnown)
	return p
}

// Len is the number of dial targets in a pass: every endpoint, plus the mDNS
// slot when it is on.
func (p Plan) Len() int {
	n := len(p.Endpoints)
	if p.MDNS {
		n++
	}
	return n
}

// StickTo returns the endpoint a dropped link reconnects to instead of
// rerunning the cascade: the one that last registered, while the grace has
// not run out and the plan still lists it. The plan's entry is returned rather
// than the pinned copy, so a config edit to that address takes effect now. An
// endpoint with no TLS port cannot register while a CA is installed, so that
// case falls through to the mDNS slot that supplies one.
func (p Plan) StickTo(pinned *ServerInfo, since, now time.Time, needTLS bool) *ServerInfo {
	if pinned == nil || now.Sub(since) >= StickyGrace {
		return nil
	}
	for _, ep := range p.Endpoints {
		if ep.Addr != pinned.Addr {
			continue
		}
		if needTLS && ep.TLSPort == 0 {
			return nil
		}
		return ep
	}
	return nil
}
