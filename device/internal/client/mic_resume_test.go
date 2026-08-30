package client

import (
	"testing"

	"github.com/wilbowes/EchoMuse/internal/aec"
)

// The device went deaf on any data-plane drop the CONTROL plane survived.
// StartMic had two callers — the controller's mic_start and unmute — so
// nothing restarted a stream that died with its socket, and the controller
// had no reconnect event to fire a fresh mic_start from. Measured twice on
// Test Echo 1: 34s deaf (2026-08-29) and 41s+ (2026-08-30).
//
// The fix separates the controller's INTENT (micWanted, outlives any one
// connection) from the stream's STATE (micActive, dies with its socket).
// These cover the transitions rather than the socket plumbing, which
// data_race_test.go already exercises.

func newTestClient(t *testing.T) *DataClient {
	t.Helper()
	return NewDataClient("resume-test", newFanoutMic(), nil, aec.New())
}

func micIntent(d *DataClient) (want, active bool) {
	d.micMu.Lock()
	defer d.micMu.Unlock()
	return d.micWanted, d.micActive
}

func TestAMicStartWithNoConnectionIsRememberedNotDiscarded(t *testing.T) {
	// The boot race: the controller's first mic_start can beat the data
	// connection into existence. Discarding it left the device waiting to
	// be asked again — 39 seconds on 2026-08-30's boot.
	d := newTestClient(t)
	d.StartMic(false)

	want, active := micIntent(d)
	if !want {
		t.Fatal("mic_start with no connection must record the intent")
	}
	if active {
		t.Fatal("no connection — the stream cannot be active")
	}
}

func TestStopClearsTheIntentEvenWithNoStreamRunning(t *testing.T) {
	// A mic_stop or a mute arriving while the data plane is down has to
	// cancel the intent, or the next connect restores a stream the
	// controller has already ended.
	d := newTestClient(t)
	d.StartMic(false)
	d.StopMic()

	if want, _ := micIntent(d); want {
		t.Fatal("StopMic must clear the intent even when no stream is running")
	}
}

func TestResumeDoesNothingWithoutAStandingRequest(t *testing.T) {
	// A device the controller has not asked to stream must not start
	// streaming because a socket reconnected.
	d := newTestClient(t)
	d.resumeMic()

	if _, active := micIntent(d); active {
		t.Fatal("resume must not start a stream nobody asked for")
	}
}

func TestResumeAfterAStopStaysStopped(t *testing.T) {
	d := newTestClient(t)
	d.StartMic(false)
	d.StopMic()
	d.resumeMic()

	if _, active := micIntent(d); active {
		t.Fatal("a stopped mic must stay stopped across a reconnect")
	}
}

func TestTheLockFlagSurvivesForTheRestore(t *testing.T) {
	// lockMic decides whether the beamformer locks to a perimeter mic for
	// the turn. Restoring a locked stream as unlocked would silently move
	// the device back to the omni mic mid-turn.
	d := newTestClient(t)
	d.StartMic(true)

	d.micMu.Lock()
	got := d.micWantedLock
	d.micMu.Unlock()
	if !got {
		t.Fatal("lockMic must be remembered for the restore")
	}

	d.StartMic(false)
	d.micMu.Lock()
	got = d.micWantedLock
	d.micMu.Unlock()
	if got {
		t.Fatal("a later unlocked request must replace the remembered flag")
	}
}
