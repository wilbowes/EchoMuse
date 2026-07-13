package client

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/aec"
)

// fanoutMic is a minimal mic.Subscribable: a background pump broadcasts raw
// 9ch S24_3LE periods to every subscriber until closed, mimicking
// PcmMicrophone's fan-out (including the drop-when-full behaviour).
type fanoutMic struct {
	mu     sync.Mutex
	subs   []chan []byte
	stopCh chan struct{}
}

func newFanoutMic() *fanoutMic {
	m := &fanoutMic{stopCh: make(chan struct{})}
	// One 512-frame period of 9ch S24_3LE (the minimum Process() analyses),
	// non-zero so the beamformer smoothers see real energy.
	raw := make([]byte, 512*9*3)
	for i := range raw {
		raw[i] = byte(i % 251)
	}
	go func() {
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-m.stopCh:
				return
			case <-ticker.C:
				m.mu.Lock()
				for _, ch := range m.subs {
					select {
					case ch <- raw:
					default:
					}
				}
				m.mu.Unlock()
			}
		}
	}()
	return m
}

func (m *fanoutMic) Subscribe() chan []byte {
	ch := make(chan []byte, 32)
	m.mu.Lock()
	m.subs = append(m.subs, ch)
	m.mu.Unlock()
	return ch
}

func (m *fanoutMic) Unsubscribe(ch chan []byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for i, s := range m.subs {
		if s == ch {
			m.subs = append(m.subs[:i], m.subs[i+1:]...)
			close(ch)
			return
		}
	}
}

func (m *fanoutMic) close() { close(m.stopCh) }

// dialTestWS stands up a WebSocket sink and returns a client conn to it.
func dialTestWS(t *testing.T) (*websocket.Conn, func()) {
	t.Helper()
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		for {
			if _, _, err := c.ReadMessage(); err != nil {
				return
			}
		}
	}))
	url := "ws://" + strings.TrimPrefix(srv.URL, "http://")
	conn, _, err := websocket.DefaultDialer.Dial(url, nil)
	if err != nil {
		srv.Close()
		t.Fatalf("dial test ws: %v", err)
	}
	return conn, func() {
		conn.Close()
		srv.Close()
	}
}

// TestStreamRestartOverlapIsRaceFree drives the exact sequence the controller
// sends after every voice turn — StopMic immediately followed by StartMic —
// while mic data is flowing. The superseded streamMic goroutine can keep
// draining periods for a few iterations after its stopCh closes (select on a
// closed channel vs a ready mic channel picks randomly), so for a window the
// old and new goroutines run concurrently against the shared beamformer and
// AGC state. Run under -race: before pipeMu serialised the pipeline this
// reliably reported races on the beamformer's reused analysis buffers, and
// the old goroutine's deferred beam.Unlock could land after the new stream's
// Lock. Beam lock/unlock requests are mixed in to cover the mid-stream
// request path too.
func TestStreamRestartOverlapIsRaceFree(t *testing.T) {
	mic := newFanoutMic()
	defer mic.close()
	conn, cleanup := dialTestWS(t)
	defer cleanup()

	d := NewDataClient("race-test", mic, nil, aec.New())
	d.connMu.Lock()
	d.conn = conn
	d.connMu.Unlock()

	for i := 0; i < 100; i++ {
		lockMic := i%2 == 0 // alternate turn stream / wake stream
		d.StartMic(lockMic)
		d.RequestBeamLock()
		time.Sleep(2 * time.Millisecond) // let a couple of periods flow
		d.RequestBeamUnlock()
		d.StopMic()
		// No settling delay: the replacement StartMic in the next iteration
		// racing the superseded goroutine's drain is the scenario under test.
	}

	d.StopMic()
	// Give lingering goroutines time to exit so their deferred cleanup runs
	// (and the race detector observes it) before the test tears down.
	time.Sleep(100 * time.Millisecond)
}
