package client

import (
	"crypto/sha256"
	"encoding/hex"
	"log"
	"os"
	"sync"
	"time"
)

// Pairing is how an Echo asks to be (re)issued link credentials: its owner
// holds the action button for PairHoldDuration, the device opens a window of
// pairWindow, and an admin's Approve in the dashboard issues a fresh token
// and the CA (controller/em_pairing.py, Wil 2026-09-26).
//
// Outside a window, a device holding a CA dials ONLY wss. Before this it fell
// back to plain whenever TLS did not work, and anyone on the LAN can make TLS
// not work, so the fallback was a downgrade anyone could trigger. Inside a
// window it may dial plain to ask, but never presents its token there: a
// device with a CA never sends its token over plain (linkCreds.headerFor).
//
// The window also closes as soon as the credential files change. The
// approval installs new ones and bounces the link, and a redial that still
// carried `pairing` would raise a second request for a device already paired.
const (
	PairHoldDuration = 5 * time.Second
	pairWindow       = 2 * time.Minute
	// How often a request is repeated while the window is open, on a live link
	// (pair_request) or by redialling. The controller drops a request 30s
	// after its last repeat (em_pairing.REQUEST_TTL_S).
	pairRepeat = 5 * time.Second
)

type pairState struct {
	mu     sync.Mutex
	until  time.Time
	fp     string // credFingerprint when the window opened
	asking bool   // the pair_request repeater is running
	kick   chan struct{}
}

func newPairState() *pairState {
	return &pairState{kick: make(chan struct{}, 1)}
}

// open starts (or restarts) the window. True when the caller should start the
// repeater, i.e. none is running.
func (p *pairState) open(now time.Time, fp string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.until = now.Add(pairWindow)
	p.fp = fp
	start := !p.asking
	p.asking = true
	return start
}

// active reports whether the window is open, closing it once it has expired
// or the credentials have changed since it opened.
func (p *pairState) active(now time.Time, fp string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.activeLocked(now, fp)
}

func (p *pairState) activeLocked(now time.Time, fp string) bool {
	if p.until.IsZero() {
		return false
	}
	if !now.Before(p.until) || fp != p.fp {
		p.until = time.Time{}
		return false
	}
	return true
}

// keepAsking is the repeater's loop test. It clears `asking` in the same
// critical section that finds the window closed, so an open() racing the
// repeater's exit always starts a new one.
func (p *pairState) keepAsking(now time.Time, fp string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.activeLocked(now, fp) {
		return true
	}
	p.asking = false
	return false
}

// credFingerprint identifies the installed credential files, so the window
// can tell that an approval has replaced them.
func credFingerprint() string {
	h := sha256.New()
	for _, path := range []string{credCAPath, credTokenPath} {
		b, _ := os.ReadFile(path)
		h.Write(b)
		h.Write([]byte{0})
	}
	return hex.EncodeToString(h.Sum(nil))
}

// StartPairing opens a pairing window. Called on a 5 s hold of the action
// button, whether or not the device is connected.
func (c *ControlClient) StartPairing() {
	log.Printf("[pair] action button held — asking to pair for %s", pairWindow)
	if c.pair.open(time.Now(), credFingerprint()) {
		go c.askToPair()
	}
	// Cut short a pending or reconnect wait, so the first request goes now.
	select {
	case c.pair.kick <- struct{}{}:
	default:
	}
}

// Pairing reports whether a pairing window is open.
func (c *ControlClient) Pairing() bool {
	return c.pair.active(time.Now(), credFingerprint())
}

// askToPair repeats pair_request on a live link for as long as the window is
// open. A device that is not connected asks by redialling instead (Run).
func (c *ControlClient) askToPair() {
	t := time.NewTicker(pairRepeat)
	defer t.Stop()
	for c.pair.keepAsking(time.Now(), credFingerprint()) {
		if c.IsConnected() {
			if err := c.writeJSON(map[string]string{"type": "pair_request"}); err != nil {
				log.Printf("[pair] pair_request failed: %v", err)
			}
		}
		<-t.C
	}
	log.Printf("[pair] pairing window closed")
}

// dialAttempt is one way of reaching the controller's control plane.
type dialAttempt struct {
	tls     bool
	pairing bool // register carries "pairing": true
}

// dialPlan is the ordered list of dials for one connection attempt. Empty
// means there is nothing this device may dial: it holds a CA, the controller
// advertises no TLS listener, and no pairing window is open.
func dialPlan(hasCA bool, tlsPort int, pairing bool) []dialAttempt {
	if !hasCA {
		return []dialAttempt{{tls: false, pairing: pairing}}
	}
	var plan []dialAttempt
	if tlsPort > 0 {
		plan = append(plan, dialAttempt{tls: true, pairing: pairing})
	}
	if pairing {
		// wss failed to connect (a regenerated CA, a different controller),
		// so ask over plain. headerFor keeps the token off this dial.
		plan = append(plan, dialAttempt{tls: false, pairing: true})
	}
	return plan
}

// PairHold turns a long hold of the action button into StartPairing. It sits
// in front of the link-down gate in cmd, since the device that most needs it
// is the one that cannot connect.
type PairHold struct {
	mu    sync.Mutex
	hold  time.Duration
	fire  func()
	timer *time.Timer
	gen   uint64 // bumped on every press and release; a stale timer is ignored
	fired bool
}

func NewPairHold(fire func()) *PairHold {
	return &PairHold{hold: PairHoldDuration, fire: fire}
}

// Event takes each action-button edge and reports whether to swallow it. The
// press is never swallowed (the controller ignores presses); the release that
// ends a pairing hold is, so the hold is not also sent to HA as a long press.
func (h *PairHold) Event(down bool) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.gen++
	if h.timer != nil {
		h.timer.Stop()
		h.timer = nil
	}
	if down {
		h.fired = false
		gen := h.gen
		h.timer = time.AfterFunc(h.hold, func() {
			h.mu.Lock()
			if h.gen != gen {
				h.mu.Unlock()
				return
			}
			h.fired = true
			h.mu.Unlock()
			h.fire()
		})
		return false
	}
	swallow := h.fired
	h.fired = false
	return swallow
}
