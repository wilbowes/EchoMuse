package bluetooth

// The controller's side of the connection manager (#656): requests in as
// JSON, results and events out as JSON, carried by whatever the caller has —
// the data plane's ble-gatt frame in the firmware, a slice in the tests.
// docs/device-controller-interface.md has the contract.
//
// Requests for one peer run in the order they arrived, on that peer's own
// worker: Home Assistant writes a command and then reads its result, and two
// goroutines racing for the link would let the read go first. Different
// peers do not wait for each other — a connect can take 20s.

import (
	"encoding/json"
	"errors"
	"sync"
	"sync/atomic"
)

const bridgeQueueDepth = 32

type bridgeRequest struct {
	T        string `json:"t"`
	Req      uint32 `json:"req"`
	Addr     string `json:"addr"`
	AddrType int    `json:"addr_type"`
	Handle   uint16 `json:"handle"`
	Value    []byte `json:"value"`
	Response bool   `json:"response"`
}

type wireDescriptor struct {
	UUID   string `json:"uuid"`
	Handle uint16 `json:"handle"`
}

type wireCharacteristic struct {
	UUID        string           `json:"uuid"`
	Handle      uint16           `json:"handle"`
	ValueHandle uint16           `json:"value_handle"`
	Properties  byte             `json:"props"`
	Descriptors []wireDescriptor `json:"descs"`
}

type wireService struct {
	UUID            string               `json:"uuid"`
	Start           uint16               `json:"start"`
	End             uint16               `json:"end"`
	Characteristics []wireCharacteristic `json:"chars"`
}

type bridgeResult struct {
	T     string `json:"t"` // "result"
	Req   uint32 `json:"req"`
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
	// ATT is the peer's ATT error code when Error is "att".
	ATT      *int          `json:"att,omitempty"`
	Detail   string        `json:"detail,omitempty"`
	MTU      int           `json:"mtu,omitempty"`
	Value    *[]byte       `json:"value,omitempty"`
	Services []wireService `json:"services,omitempty"`
}

type bridgeWorker struct {
	q chan bridgeRequest
}

// Bridge serves the controller's GATT requests against a ConnManager.
type Bridge struct {
	m       *ConnManager
	send    func([]byte)
	enabled atomic.Bool

	mu      sync.Mutex
	workers map[string]*bridgeWorker
}

// NewBridge takes over the manager's callbacks. send must not block for long
// and may be called from several goroutines.
func NewBridge(m *ConnManager, send func([]byte)) *Bridge {
	b := &Bridge{m: m, send: send, workers: map[string]*bridgeWorker{}}
	m.OnNotify = func(addr string, handle uint16, value []byte, indication bool) {
		b.emit(map[string]any{"t": "notify", "addr": addr, "handle": handle, "value": value, "ind": indication})
	}
	m.OnDisconnect = func(addr string, reason byte) {
		b.retire(addr)
		b.emit(map[string]any{"t": "disconnected", "addr": addr, "reason": int(reason)})
		b.sendSlots(nil)
	}
	return b
}

// SetEnabled follows the bleProxyConnections setting. Turning it off ends
// every link: a setting that says no connections must not leave one up.
func (b *Bridge) SetEnabled(enabled bool) {
	if b.enabled.Swap(enabled) == enabled {
		return
	}
	if !enabled {
		b.DropAll()
	}
	b.sendSlots(nil)
}

// DropAll ends every link. Called when the controller link goes away too:
// nothing can use a connection whose results have nowhere to go, and the
// peer's slot is better freed than held.
func (b *Bridge) DropAll() {
	for _, addr := range b.m.Connected() {
		go b.m.Disconnect(addr)
	}
}

func (b *Bridge) emit(v any) {
	if raw, err := json.Marshal(v); err == nil {
		b.send(raw)
	}
}

func (b *Bridge) sendSlots(req *uint32) {
	free, limit := b.m.Slots()
	if !b.enabled.Load() {
		free = 0
	}
	addrs := b.m.Connected()
	if addrs == nil {
		addrs = []string{}
	}
	msg := map[string]any{"t": "slots", "free": free, "limit": limit, "addrs": addrs}
	if req != nil {
		msg["req"] = *req
	}
	b.emit(msg)
}

func (b *Bridge) retire(addr string) {
	b.mu.Lock()
	if w := b.workers[addr]; w != nil {
		delete(b.workers, addr)
		close(w.q) // the worker drains what is queued, then exits
	}
	b.mu.Unlock()
}

func (b *Bridge) fail(req uint32, code string) {
	b.emit(bridgeResult{T: "result", Req: req, Error: code})
}

// Handle takes one message from the controller. Malformed input is dropped:
// there is no request id to answer.
func (b *Bridge) Handle(raw []byte) {
	var r bridgeRequest
	if err := json.Unmarshal(raw, &r); err != nil || r.T == "" {
		return
	}
	if r.T == "slots" {
		b.sendSlots(&r.Req)
		return
	}
	addr, ok := normaliseAddr(r.Addr)
	if !ok {
		b.fail(r.Req, "bad_request")
		return
	}
	r.Addr = addr
	switch r.T {
	case "connect", "disconnect", "services", "read", "write":
	default:
		b.fail(r.Req, "bad_request")
		return
	}
	if r.T == "connect" && !b.enabled.Load() {
		b.fail(r.Req, "disabled")
		return
	}

	b.mu.Lock()
	w := b.workers[addr]
	if w == nil {
		if r.T != "connect" {
			b.mu.Unlock()
			if r.T == "disconnect" {
				// Already gone, which is what was asked for.
				b.emit(bridgeResult{T: "result", Req: r.Req, OK: true})
			} else {
				b.fail(r.Req, "not_connected")
			}
			return
		}
		w = &bridgeWorker{q: make(chan bridgeRequest, bridgeQueueDepth)}
		b.workers[addr] = w
		go b.work(w)
	}
	select {
	case w.q <- r:
		b.mu.Unlock()
	default:
		b.mu.Unlock()
		b.fail(r.Req, "busy")
	}
}

func (b *Bridge) work(w *bridgeWorker) {
	for r := range w.q {
		b.run(r)
	}
}

func (b *Bridge) run(r bridgeRequest) {
	res := bridgeResult{T: "result", Req: r.Req}
	var err error
	switch r.T {
	case "connect":
		var info ConnInfo
		if info, err = b.m.Connect(r.Addr, r.AddrType); err == nil {
			res.MTU = info.MTU
		} else if !errors.Is(err, ErrAlreadyConnected) {
			b.retire(r.Addr)
		}
	case "disconnect":
		if err = b.m.Disconnect(r.Addr); errors.Is(err, ErrNotConnected) {
			err = nil
			b.retire(r.Addr)
		}
	case "services":
		var services []Service
		if services, err = b.m.Services(r.Addr); err == nil {
			res.Services = toWireServices(services)
		}
	case "read":
		var v []byte
		if v, err = b.m.Read(r.Addr, r.Handle); err == nil {
			if v == nil {
				v = []byte{}
			}
			res.Value = &v
		}
	case "write":
		err = b.m.Write(r.Addr, r.Handle, r.Value, r.Response)
	}
	if err != nil {
		res.Error, res.ATT, res.Detail = bridgeError(err)
	} else {
		res.OK = true
	}
	b.emit(res)
	if r.T == "connect" {
		b.sendSlots(nil)
	}
}

// bridgeError names an error for the controller. The names are the contract;
// detail is for the log.
func bridgeError(err error) (code string, att *int, detail string) {
	var e *ATTError
	switch {
	case errors.As(err, &e):
		n := int(e.Code)
		return "att", &n, ""
	case errors.Is(err, ErrNoSlots):
		return "no_slots", nil, ""
	case errors.Is(err, ErrConnectTimeout):
		return "timeout", nil, ""
	case errors.Is(err, ErrAlreadyConnected):
		return "already_connected", nil, ""
	case errors.Is(err, ErrNotConnected):
		return "not_connected", nil, ""
	case errors.Is(err, ErrDisconnected):
		return "disconnected", nil, ""
	case errors.Is(err, ErrATTTimeout):
		return "att_timeout", nil, ""
	case errors.Is(err, ErrNotRunning):
		return "not_running", nil, ""
	case errors.Is(err, ErrTooLong):
		return "too_long", nil, ""
	}
	return "failed", nil, err.Error()
}

func toWireServices(services []Service) []wireService {
	out := make([]wireService, 0, len(services))
	for _, s := range services {
		ws := wireService{UUID: s.UUID.String(), Start: s.Start, End: s.End, Characteristics: []wireCharacteristic{}}
		for _, c := range s.Characteristics {
			wc := wireCharacteristic{
				UUID: c.UUID.String(), Handle: c.Handle, ValueHandle: c.ValueHandle,
				Properties: c.Properties, Descriptors: []wireDescriptor{},
			}
			for _, d := range c.Descriptors {
				wc.Descriptors = append(wc.Descriptors, wireDescriptor{UUID: d.UUID.String(), Handle: d.Handle})
			}
			ws.Characteristics = append(ws.Characteristics, wc)
		}
		out = append(out, ws)
	}
	return out
}
