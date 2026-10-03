package bluetooth

import (
	"encoding/json"
	"fmt"
	"sync"
	"testing"
	"time"
)

// wire collects what the bridge sends the controller.
type wire struct {
	mu   sync.Mutex
	msgs []map[string]any
}

func (w *wire) send(raw []byte) {
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		panic(err)
	}
	w.mu.Lock()
	w.msgs = append(w.msgs, m)
	w.mu.Unlock()
}

// wait returns the first message matching, within 2s.
func (w *wire) wait(t *testing.T, what string, match func(map[string]any) bool) map[string]any {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		w.mu.Lock()
		for _, m := range w.msgs {
			if match(m) {
				w.mu.Unlock()
				return m
			}
		}
		n := len(w.msgs)
		w.mu.Unlock()
		if time.Now().After(deadline) {
			t.Fatalf("no %s among %d messages: %v", what, n, w.msgs)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

func (w *wire) result(t *testing.T, req int) map[string]any {
	t.Helper()
	return w.wait(t, fmt.Sprintf("result %d", req), func(m map[string]any) bool {
		return m["t"] == "result" && m["req"] == float64(req)
	})
}

func bridge(t *testing.T, f *fakeCtl) (*Bridge, *wire) {
	t.Helper()
	w := &wire{}
	var b *Bridge
	session(t, f, func(s *Scanner) { b = NewBridge(s.Conns(), w.send) })
	b.SetEnabled(true)
	return b, w
}

func ask(b *Bridge, format string, args ...any) { b.Handle([]byte(fmt.Sprintf(format, args...))) }

func TestBridgeConnectDiscoverReadWriteNotify(t *testing.T) {
	f := newFakeCtl()
	p := f.peer(peerA)
	name, _, long, _ := table(p.srv)
	b, w := bridge(t, f)

	ask(b, `{"t":"connect","req":1,"addr":"C0:00:00:00:00:0A","addr_type":1}`)
	if r := w.result(t, 1); r["ok"] != true || r["mtu"] != float64(attDefaultMTU) {
		t.Fatalf("connect %v", r)
	}
	slots := w.wait(t, "slots after connect", func(m map[string]any) bool {
		return m["t"] == "slots" && m["free"] == float64(2)
	})
	if slots["limit"] != float64(3) || fmt.Sprint(slots["addrs"]) != "["+peerA+"]" {
		t.Fatalf("slots %v", slots)
	}

	ask(b, `{"t":"services","req":2,"addr":"%s"}`, peerA)
	r := w.result(t, 2)
	services := r["services"].([]any)
	if r["ok"] != true || len(services) != 2 {
		t.Fatalf("services %v", r)
	}
	gap := services[0].(map[string]any)
	ch := gap["chars"].([]any)[0].(map[string]any)
	if gap["uuid"] != "00001800-0000-1000-8000-00805f9b34fb" || gap["start"] != float64(1) ||
		ch["uuid"] != "00002a00-0000-1000-8000-00805f9b34fb" || ch["value_handle"] != float64(name) ||
		ch["props"] != float64(2) || len(ch["descs"].([]any)) != 0 {
		t.Fatalf("first service %v", gap)
	}
	custom := services[1].(map[string]any)
	if custom["uuid"] != uuid128Text {
		t.Fatalf("128-bit uuid %v", custom["uuid"])
	}
	if d := custom["chars"].([]any)[0].(map[string]any)["descs"].([]any); len(d) != 1 ||
		d[0].(map[string]any)["uuid"] != "00002902-0000-1000-8000-00805f9b34fb" {
		t.Fatalf("descriptors %v", d)
	}

	// Values travel as base64, which is what Go and Python both make of bytes.
	ask(b, `{"t":"read","req":3,"addr":"%s","handle":%d}`, peerA, name)
	if r := w.result(t, 3); r["ok"] != true || r["value"] != "Q2hvbmt5IE1vbmtleQ==" {
		t.Fatalf("read %v", r)
	}
	ask(b, `{"t":"write","req":4,"addr":"%s","handle":%d,"value":"R29vZCBnb2xseQ==","response":true}`, peerA, long)
	if r := w.result(t, 4); r["ok"] != true {
		t.Fatalf("write %v", r)
	}
	if got := string(p.srv.attrs[long-1].value); got != "Good golly" {
		t.Fatalf("peer holds %q", got)
	}
	// An empty value is a value, not a missing one.
	p.srv.attrs[long-1].value = nil
	ask(b, `{"t":"read","req":5,"addr":"%s","handle":%d}`, peerA, long)
	if r := w.result(t, 5); r["ok"] != true || r["value"] != "" {
		t.Fatalf("empty read %v", r)
	}

	// A refusal carries the peer's ATT code.
	ask(b, `{"t":"read","req":6,"addr":"%s","handle":30000}`, peerA)
	if r := w.result(t, 6); r["ok"] != false || r["error"] != "att" || r["att"] != float64(1) {
		t.Fatalf("att error %v", r)
	}

	f.toHost(p.handle, cidATT, append([]byte{attOpNotification, byte(long), 0}, "hi"...))
	n := w.wait(t, "notify", func(m map[string]any) bool { return m["t"] == "notify" })
	if n["addr"] != peerA || n["handle"] != float64(long) || n["value"] != "aGk=" || n["ind"] != false {
		t.Fatalf("notify %v", n)
	}

	ask(b, `{"t":"disconnect","req":7,"addr":"%s"}`, peerA)
	if r := w.result(t, 7); r["ok"] != true {
		t.Fatalf("disconnect %v", r)
	}
	d := w.wait(t, "disconnected", func(m map[string]any) bool { return m["t"] == "disconnected" })
	if d["addr"] != peerA || d["reason"] != float64(hciReasonLocalHost) {
		t.Fatalf("disconnected %v", d)
	}
	w.wait(t, "slots after disconnect", func(m map[string]any) bool {
		return m["t"] == "slots" && m["free"] == float64(3) && len(m["addrs"].([]any)) == 0
	})
	// Disconnecting what is already gone is not an error.
	ask(b, `{"t":"disconnect","req":8,"addr":"%s"}`, peerA)
	if r := w.result(t, 8); r["ok"] != true {
		t.Fatalf("second disconnect %v", r)
	}
}

func TestBridgeKeepsOnePeersRequestsInOrder(t *testing.T) {
	f := newFakeCtl()
	p := f.peer(peerA)
	_, _, long, _ := table(p.srv)
	b, w := bridge(t, f)
	// Everything queued before the connect has even completed.
	ask(b, `{"t":"connect","req":1,"addr":"%s","addr_type":1}`, peerA)
	for i := 0; i < 10; i++ {
		ask(b, `{"t":"write","req":%d,"addr":"%s","handle":%d,"value":"%s","response":%v}`,
			10+i, peerA, long, []string{"QQ==", "Qg=="}[i%2], i%3 == 0)
		ask(b, `{"t":"read","req":%d,"addr":"%s","handle":%d}`, 100+i, peerA, long)
	}
	for i := 0; i < 10; i++ {
		want := []string{"QQ==", "Qg=="}[i%2]
		if r := w.result(t, 100+i); r["value"] != want {
			t.Fatalf("read %d saw %v, want %s: a read overtook its write", i, r["value"], want)
		}
	}
}

func TestBridgeRefusals(t *testing.T) {
	f := newFakeCtl()
	for _, a := range []string{peerA, peerB, peerC, peerD} {
		f.peer(a)
	}
	f.peers[peerD].silent = true
	w := &wire{}
	var b *Bridge
	session(t, f, func(s *Scanner) {
		s.Conns().connectTimeout = 30 * time.Millisecond
		b = NewBridge(s.Conns(), w.send)
	})

	// Off until the setting says otherwise, and it says so with no free slots.
	ask(b, `{"t":"connect","req":1,"addr":"%s","addr_type":1}`, peerA)
	if r := w.result(t, 1); r["error"] != "disabled" {
		t.Fatalf("disabled %v", r)
	}
	ask(b, `{"t":"slots","req":2}`)
	if r := w.wait(t, "slots reply", func(m map[string]any) bool { return m["t"] == "slots" && m["req"] == float64(2) }); r["free"] != float64(0) || r["limit"] != float64(3) {
		t.Fatalf("slots while disabled %v", r)
	}
	b.SetEnabled(true)

	for req, c := range map[int]struct{ msg, want string }{
		10: {`{"t":"read","req":10,"addr":"` + peerA + `","handle":1}`, "not_connected"},
		11: {`{"t":"connect","req":11,"addr":"not an address"}`, "bad_request"},
		12: {`{"t":"explode","req":12,"addr":"` + peerA + `"}`, "bad_request"},
		13: {`{"t":"connect","req":13,"addr":"` + peerD + `","addr_type":1}`, "timeout"},
	} {
		ask(b, "%s", c.msg)
		if r := w.result(t, req); r["ok"] != false || r["error"] != c.want {
			t.Fatalf("req %d: %v, want %s", req, r, c.want)
		}
	}
	// Not JSON, and JSON with no type: nothing to answer, nothing sent.
	w.mu.Lock()
	before := len(w.msgs)
	w.mu.Unlock()
	b.Handle([]byte(`{"t":`))
	b.Handle([]byte(`{"req":99}`))
	b.Handle(nil)
	time.Sleep(20 * time.Millisecond)
	w.mu.Lock()
	if len(w.msgs) != before {
		t.Fatalf("answered garbage: %v", w.msgs[before:])
	}
	w.mu.Unlock()

	for i, a := range []string{peerA, peerB, peerC} {
		ask(b, `{"t":"connect","req":%d,"addr":"%s","addr_type":1}`, 20+i, a)
		if r := w.result(t, 20+i); r["ok"] != true {
			t.Fatalf("connect %s: %v", a, r)
		}
	}
	f.peers[peerD].silent = false
	ask(b, `{"t":"connect","req":30,"addr":"%s","addr_type":1}`, peerD)
	if r := w.result(t, 30); r["error"] != "no_slots" {
		t.Fatalf("fourth %v", r)
	}
	ask(b, `{"t":"connect","req":31,"addr":"%s","addr_type":1}`, peerA)
	if r := w.result(t, 31); r["error"] != "already_connected" {
		t.Fatalf("again %v", r)
	}
	// The refused duplicate must not have cost the live link its worker.
	ask(b, `{"t":"read","req":32,"addr":"%s","handle":1}`, peerA)
	if r := w.result(t, 32); r["error"] != "att" {
		t.Fatalf("link after a duplicate connect: %v", r)
	}

	// Switching the setting off ends every link.
	b.SetEnabled(false)
	w.wait(t, "all links down", func(m map[string]any) bool {
		return m["t"] == "slots" && len(m["addrs"].([]any)) == 0
	})
	for _, a := range []string{peerA, peerB, peerC} {
		w.wait(t, "disconnected "+a, func(m map[string]any) bool { return m["t"] == "disconnected" && m["addr"] == a })
	}
}

func TestBridgeReportsAPeerDropping(t *testing.T) {
	f := newFakeCtl()
	p := f.peer(peerA)
	b, w := bridge(t, f)
	ask(b, `{"t":"connect","req":1,"addr":"%s","addr_type":1}`, peerA)
	w.result(t, 1)
	p.mute = true
	ask(b, `{"t":"read","req":2,"addr":"%s","handle":1}`, peerA)
	ask(b, `{"t":"read","req":3,"addr":"%s","handle":1}`, peerA)
	time.Sleep(20 * time.Millisecond)
	f.drop(p.handle, 0x08)
	if r := w.result(t, 2); r["error"] != "disconnected" {
		t.Fatalf("in flight %v", r)
	}
	if r := w.result(t, 3); r["error"] != "not_connected" && r["error"] != "disconnected" {
		t.Fatalf("queued behind it %v", r)
	}
	d := w.wait(t, "disconnected", func(m map[string]any) bool { return m["t"] == "disconnected" })
	if d["reason"] != float64(0x08) {
		t.Fatalf("%v", d)
	}
	// And the address can be connected again.
	p.mute = false
	ask(b, `{"t":"connect","req":4,"addr":"%s","addr_type":1}`, peerA)
	if r := w.result(t, 4); r["ok"] != true {
		t.Fatalf("reconnect %v", r)
	}
}
