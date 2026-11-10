package client

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/aec"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// recordingSpeaker is a speaker.Speaker that records what the data client
// asks of it.
type recordingSpeaker struct {
	mu        sync.Mutex
	music     int
	abandoned []time.Time
}

func (r *recordingSpeaker) Init() error             { return nil }
func (r *recordingSpeaker) PumpPeriod([]byte) error { return nil }
func (r *recordingSpeaker) EndStream()              {}
func (r *recordingSpeaker) Flush()                  {}
func (r *recordingSpeaker) EndMusicStream()         {}
func (r *recordingSpeaker) FlushMusic()             {}
func (r *recordingSpeaker) SetDuck(float64)         {}
func (r *recordingSpeaker) Close()                  {}
func (r *recordingSpeaker) PumpMusic(b []byte) error {
	r.mu.Lock()
	r.music++
	r.mu.Unlock()
	return nil
}
func (r *recordingSpeaker) AbandonStreams(at time.Time) {
	r.mu.Lock()
	r.abandoned = append(r.abandoned, at)
	r.mu.Unlock()
}

// 2026-10-23: a Dot's data link dropped mid-song and the music stream's EOS
// went with it. The stream stayed active for four days. When the link that
// carried a stream closes, connect must tell the speaker to abandon it, at a
// time no earlier than the last period it delivered.
func TestALinkLostMidSongAbandonsTheStream(t *testing.T) {
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		c.ReadMessage() // identify
		c.WriteMessage(websocket.BinaryMessage, []byte{frameTypeMusic, 1, 2, 3, 4})
		// No music EOS: the link goes down mid-song.
		c.UnderlyingConn().Close()
	}))
	defer srv.Close()
	addr := "ws://" + strings.TrimPrefix(srv.URL, "http://")

	mic := newFanoutMic()
	defer mic.close()
	spk := &recordingSpeaker{}
	d := NewDataClient("link-loss-test", mic, spk, aec.New(board.BiscuitMics().NumMics()))

	start := time.Now()
	done := make(chan error, 1)
	go func() { done <- d.connect(context.Background(), addr) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("connect did not return after the link closed")
	}

	spk.mu.Lock()
	defer spk.mu.Unlock()
	if spk.music != 1 {
		t.Fatalf("precondition: one music period delivered, got %d", spk.music)
	}
	if len(spk.abandoned) != 1 {
		t.Fatalf("AbandonStreams called %d times when the link closed, want 1", len(spk.abandoned))
	}
	if spk.abandoned[0].Before(start) {
		t.Fatal("linkLost must not predate the period the link delivered")
	}
}
