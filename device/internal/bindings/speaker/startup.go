package speaker

import (
	"fmt"
	"strings"
	"time"
)

// waitForSilence fails if the writer exits, including when both the timer and
// the writer have completed. A delay alone is not evidence of a live stream.
func waitForSilence(dead <-chan struct{}, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-dead:
		return fmt.Errorf("speaker: silence stream stopped during startup")
	case <-timer.C:
		select {
		case <-dead:
			return fmt.Errorf("speaker: silence stream stopped during startup")
		default:
			return nil
		}
	}
}

// The first line is the PCM state; do not accept RUNNING found elsewhere in
// the status text. Bounded and injectable so failed opens/reads are testable.
func waitForRunningPCM(read func() ([]byte, error), dead <-chan struct{}, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		select {
		case <-dead:
			return fmt.Errorf("speaker: playback writer stopped")
		default:
		}
		b, err := read()
		if err != nil {
			return fmt.Errorf("speaker: read playback status: %w", err)
		}
		first, _, _ := strings.Cut(string(b), "\n")
		key, state, ok := strings.Cut(first, ":")
		if ok && strings.TrimSpace(key) == "state" && strings.TrimSpace(state) == "RUNNING" {
			return nil
		}
		if !time.Now().Before(deadline) {
			return fmt.Errorf("speaker: playback did not start")
		}
		if err := waitForSilence(dead, 10*time.Millisecond); err != nil {
			return err
		}
	}
}
