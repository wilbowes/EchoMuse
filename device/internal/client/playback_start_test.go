package client

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// playback_started (#203): the controller's Speaking tile and playback meter
// ring key off this message instead of estimating the moment from the first
// period they put on the wire, which leads the speaker by up to the prime
// window. So what it carries matters as much as that it arrives: the type
// string the controller dispatches on, and nothing else.
func TestPlaybackStartedIsOneMessageAndNoPayload(t *testing.T) {
	got := make(chan string, 1)
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		if _, msg, err := c.ReadMessage(); err == nil {
			got <- string(msg)
		}
	}))
	defer srv.Close()

	conn, _, err := websocket.DefaultDialer.Dial(
		"ws://"+strings.TrimPrefix(srv.URL, "http://"), nil)
	if err != nil {
		t.Fatalf("dial test ws: %v", err)
	}
	defer conn.Close()

	c := &ControlClient{deviceID: "playback-start-test"}
	c.connMu.Lock()
	c.conn = conn
	c.connMu.Unlock()
	c.SendPlaybackStarted()

	select {
	case raw := <-got:
		var msg map[string]any
		if err := json.Unmarshal([]byte(raw), &msg); err != nil {
			t.Fatalf("playback_started is not JSON: %v (%s)", err, raw)
		}
		if msg["type"] != "playback_started" {
			t.Errorf("type = %v, want playback_started — the controller "+
				"dispatches on this string, and a typo is a message no "+
				"controller acts on", msg["type"])
		}
		// No payload is deliberate: the device's wall clock is not
		// trustworthy before NTP, so a timestamp would be worse than
		// useless, and one stream is in flight at a time.
		if len(msg) != 1 {
			t.Errorf("playback_started carries %d fields (%s); the report is "+
				"the type alone", len(msg), raw)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no playback_started reached the controller")
	}
}

// The controller must only fall back to its own estimate on firmware that
// cannot do better, so it gates the report on this capability. A typo here is
// silent on this side and permanent: every device announcing it stops being
// believed. controller/tests/test_capabilities.py reads this function and
// fails if the string the controller checks is not in it.
func TestPlaybackStartIsAnnounced(t *testing.T) {
	if !slices.Contains(capabilities(), "playback_start") {
		t.Errorf("capabilities() no longer announces playback_start: %v",
			capabilities())
	}
}
