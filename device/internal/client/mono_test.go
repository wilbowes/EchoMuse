package client

import (
	"testing"
	"time"
)

func TestMonoMsCountsFromTheProcessEpoch(t *testing.T) {
	if got := MonoMs(monoEpoch.Add(1500 * time.Millisecond)); got != 1500 {
		t.Fatalf("MonoMs = %d, want 1500", got)
	}
	a := MonoMs(time.Now())
	time.Sleep(20 * time.Millisecond)
	if d := MonoMs(time.Now()) - a; d < 20 || d > 500 {
		t.Fatalf("20ms apart read as %dms", d)
	}
}
