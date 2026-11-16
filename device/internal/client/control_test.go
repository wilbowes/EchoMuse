package client

import (
	"testing"
	"time"
)

func TestStaticRetryDelay(t *testing.T) {
	tests := []struct {
		passNum int
		want    time.Duration
	}{
		{0, 5 * time.Second},
		{1, 5 * time.Second},
		{2, 10 * time.Second},
		{3, 20 * time.Second},
		{4, 60 * time.Second},
		{5, 60 * time.Second},   // holds at the last value
		{100, 60 * time.Second}, // still holds, however many passes have failed
	}

	for _, tt := range tests {
		if got := staticRetryDelay(tt.passNum); got != tt.want {
			t.Errorf("staticRetryDelay(%d) = %v, want %v", tt.passNum, got, tt.want)
		}
	}
}

// Identify (#860) has no fallback: the controller offers it only to firmware
// that announces this, so dropping the name silently disables the dashboard
// control on every Echo.
func TestIdentifyRingIsAnnounced(t *testing.T) {
	for _, c := range capabilities() {
		if c == "identify_ring" {
			return
		}
	}
	t.Fatal("capabilities() does not announce identify_ring")
}

func TestIdentifyMessageRunsTheCallback(t *testing.T) {
	c := &ControlClient{}
	called := 0
	c.OnIdentify(func() { called++ })
	c.identifyCallback()
	if called != 1 {
		t.Fatalf("callback ran %d times", called)
	}
}
