package speaker

import (
	"errors"
	"testing"
	"time"
)

func TestPlaybackStartupGate(t *testing.T) {
	live := make(chan struct{})
	for _, s := range []string{"state: RUNNING\nowner_pid: 1", "state:\tRUNNING\n"} {
		if err := waitForRunningPCM(func() ([]byte, error) { return []byte(s), nil }, live, 0); err != nil {
			t.Fatal(err)
		}
	}
	for _, s := range []string{"closed", "state: PREPARED", "state: XRUN\nprevious state: RUNNING", "state: NOT_RUNNING"} {
		if err := waitForRunningPCM(func() ([]byte, error) { return []byte(s), nil }, live, 0); err == nil {
			t.Fatalf("accepted %q", s)
		}
	}
	calls := 0
	if err := waitForRunningPCM(func() ([]byte, error) {
		calls++
		if calls == 1 {
			return []byte("state: PREPARED"), nil
		}
		return []byte("state: RUNNING"), nil
	}, live, time.Second); err != nil {
		t.Fatal(err)
	}
	sentinel := errors.New("status read failed")
	if err := waitForRunningPCM(func() ([]byte, error) { return nil, sentinel }, live, time.Second); !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
	close(live)
	if err := waitForRunningPCM(func() ([]byte, error) { t.Fatal("read after writer died"); return nil, nil }, live, time.Second); err == nil {
		t.Fatal("accepted dead writer")
	}
	for i := 0; i < 100; i++ {
		if err := waitForSilence(live, 0); err == nil {
			t.Fatal("timer won over dead writer")
		}
	}
}
