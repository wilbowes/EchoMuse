package client

import (
	"testing"

	"github.com/wilbowes/EchoMuse/internal/aec"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// The controller's mic_start / mic_stop against mute and private listening
// (mic_commands.go). Mutant G09 of the 2026-11-16 test review: accepting
// mic_start while muted survived every suite, because the decision lived in
// cmd/ where no host test can reach it.

func newConnectedClient(t *testing.T) *DataClient {
	t.Helper()
	mic := newFanoutMic()
	conn, cleanup := dialTestWS(t)
	d := NewDataClient("mic-cmd-test", mic, nil, aec.New(board.BiscuitMics().NumMics()))
	d.connMu.Lock()
	d.conn = conn
	d.connMu.Unlock()
	t.Cleanup(func() {
		d.StopMic()
		cleanup()
		mic.close()
	})
	return d
}

func micState(d *DataClient) (want, active, lock bool) {
	d.micMu.Lock()
	defer d.micMu.Unlock()
	return d.micWanted, d.micActive, d.micWantedLock
}

func TestMicStartWhileMutedIsRefusedAndLeavesNoIntent(t *testing.T) {
	for _, lockMic := range []bool{false, true} {
		d := newConnectedClient(t)
		if d.ControllerMicStart(true, lockMic) {
			t.Fatalf("lockMic=%v: mic_start accepted while muted", lockMic)
		}
		want, active, _ := micState(d)
		if active {
			t.Fatalf("lockMic=%v: a stream started while muted", lockMic)
		}
		// An intent recorded here would be carried out by the next
		// reconnect (resumeMic), still muted.
		if want {
			t.Fatalf("lockMic=%v: intent recorded while muted", lockMic)
		}
		d.resumeMic()
		if _, active, _ := micState(d); active {
			t.Fatalf("lockMic=%v: reconnect started a stream refused under mute", lockMic)
		}
	}
}

func TestMicStartUnmutedStreams(t *testing.T) {
	d := newConnectedClient(t)
	if !d.ControllerMicStart(false, false) {
		t.Fatal("mic_start refused while unmuted")
	}
	if want, active, _ := micState(d); !want || !active {
		t.Fatalf("want=%v active=%v, expected a running stream", want, active)
	}
}

func TestButtonTurnReplacesThePrivateWakeStream(t *testing.T) {
	d := newConnectedClient(t)
	d.SetListenState(ListenLocal)
	d.ControllerMicStart(false, false) // the device's wake stream
	d.micMu.Lock()
	wakeStream := d.micStopCh
	d.micMu.Unlock()
	d.ControllerMicStart(false, true) // a button turn
	// micWantedLock is recorded even by an ignored StartMic, so ask whether
	// the stream itself was replaced: a new one has a new stop channel.
	d.micMu.Lock()
	replaced, active := d.micStopCh != wakeStream, d.micActive
	d.micMu.Unlock()
	if !active || !replaced {
		t.Fatalf("active=%v replaced=%v: lock_mic must replace the wake stream, not be ignored", active, replaced)
	}
}

func TestMicStopUnderPrivateListeningHandsBackToTheWakeStream(t *testing.T) {
	d := newConnectedClient(t)
	d.SetListenState(ListenLocal)
	d.ControllerMicStart(false, true)
	d.ControllerMicStop(false)
	if want, active, lock := micState(d); !want || !active || lock {
		t.Fatalf("want=%v active=%v lock=%v: turn end must restart the wake stream", want, active, lock)
	}
}

func TestMicStopWhileMutedDoesNotRestartTheWakeStream(t *testing.T) {
	d := newConnectedClient(t)
	d.SetListenState(ListenLocal)
	d.ControllerMicStart(false, true)
	d.ControllerMicStop(true)
	if want, active, _ := micState(d); want || active {
		t.Fatalf("want=%v active=%v: a turn ending under mute restarted the mic", want, active)
	}
}

func TestMicStopUnderPrivateListeningLeavesTheWakeStreamAlone(t *testing.T) {
	d := newConnectedClient(t)
	d.SetListenState(ListenLocal)
	d.ControllerMicStart(false, false)
	d.ControllerMicStop(false)
	if _, active, lock := micState(d); !active || lock {
		t.Fatalf("active=%v lock=%v: mic_stop must not end local listening", active, lock)
	}
}

func TestMicStopWhenStreamingStops(t *testing.T) {
	d := newConnectedClient(t)
	d.SetListenState(ListenStream)
	d.ControllerMicStart(false, false)
	d.ControllerMicStop(false)
	if want, active, _ := micState(d); want || active {
		t.Fatalf("want=%v active=%v: mic_stop must end a streaming-mode stream", want, active)
	}
}
