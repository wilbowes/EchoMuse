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

// The fake mic is loud and patterned, which the RMS threshold calls speech —
// the same mistake it makes on music residue. These pin that a turn gated by
// a model which hears no speech ends on the no-speech timeout without sending
// audio, and one that hears speech streams.

type fixedScorer float32

func (f fixedScorer) Prob([]float32) (float32, error) { return float32(f), nil }

// recordSink is a WebSocket server that keeps the first byte of every frame
// and, for mic frames (0x01 + seq), the first payload byte.
type recordSink struct {
	mu     sync.Mutex
	audio  int
	noSpch bool
}

func (r *recordSink) counts() (int, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.audio, r.noSpch
}

func runTurn(t *testing.T, scorer speechScorer, useScorer bool) (audio int, noSpeech bool) {
	t.Helper()
	noSpeechTimeoutForTest = 300 * time.Millisecond
	defer func() { noSpeechTimeoutForTest = 0 }()

	sink := &recordSink{}
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		c, err := up.Upgrade(w, req, nil)
		if err != nil {
			return
		}
		for {
			_, msg, err := c.ReadMessage()
			if err != nil {
				return
			}
			if len(msg) < 4 || msg[0] != frameTypeMic {
				continue
			}
			sink.mu.Lock()
			if len(msg) == 4 && msg[3] == frameTypeNoSpeechTimeout {
				sink.noSpch = true
			} else {
				sink.audio++
			}
			sink.mu.Unlock()
		}
	}))
	defer srv.Close()
	conn, _, err := websocket.DefaultDialer.Dial("ws://"+strings.TrimPrefix(srv.URL, "http://"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	mic := newFanoutMic()
	defer mic.close()
	d := NewDataClient("gate-test", mic, nil, aec.New())
	if useScorer {
		d.newSpeechStream = func() speechScorer { return scorer }
	} else {
		d.newSpeechStream = func() speechScorer { return nil }
	}
	d.connMu.Lock()
	d.conn = conn
	d.connMu.Unlock()

	d.StartMic(true)
	time.Sleep(700 * time.Millisecond)
	d.StopMic()
	time.Sleep(50 * time.Millisecond)
	return sink.counts()
}

func TestRMSCallsTheFakeMicSpeech(t *testing.T) {
	// The control: without this the other two prove nothing.
	audio, noSpeech := runTurn(t, nil, false)
	if audio == 0 || noSpeech {
		t.Fatalf("RMS gate should open on the fake mic: audio=%d noSpeech=%v", audio, noSpeech)
	}
}

func TestATurnTheModelHearsNoSpeechInSendsNothing(t *testing.T) {
	audio, noSpeech := runTurn(t, fixedScorer(0.03), true)
	if audio != 0 {
		t.Fatalf("sent %d audio frames for a turn with no speech", audio)
	}
	if !noSpeech {
		t.Fatal("the turn must end on the no-speech timeout")
	}
}

func TestATurnTheModelHearsSpeechInStreams(t *testing.T) {
	audio, noSpeech := runTurn(t, fixedScorer(0.9), true)
	if audio == 0 || noSpeech {
		t.Fatalf("speech must open the gate: audio=%d noSpeech=%v", audio, noSpeech)
	}
}
