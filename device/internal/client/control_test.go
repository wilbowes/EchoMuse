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
