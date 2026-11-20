package client

import "log"

// The controller's mic_start / mic_stop, decided here rather than inline in
// cmd/server.go's main(): cmd/ builds only with the hardware tags, so a
// decision left there cannot be tested on a host (test review 2026-11-16,
// mutant G09: accepting mic_start while muted survived every suite).
//
// muted is the physical mute as the caller reads it. Mute is
// device-sovereign: nothing the controller sends starts a stream under it.

// ControllerMicStart handles the controller's mic_start. Returns whether it
// was accepted.
func (d *DataClient) ControllerMicStart(muted, lockMic bool) bool {
	if muted {
		log.Println("[cmd] mic_start from controller rejected — device is muted")
		return false
	}
	// Under private listening the wake stream belongs to the device (see
	// ControllerMicStop), so a button turn's lock_mic must REPLACE it rather
	// than being refused as "already active".
	if lockMic && d.ListenState() != ListenStream && !d.TurnStreamActive() {
		d.StopMic()
	}
	d.StartMic(lockMic)
	return true
}

// ControllerMicStop handles the controller's mic_stop.
//
// Under private listening the wake stream sends nothing, and it is what hears
// a barge-in over the reply, so mic_stop ends a button or follow-up turn but
// never the local listening. A turn's end hands straight back to it, unless
// the Echo is muted.
//
// Nor does it touch a SESSION: those end only by id (listen_close). mic_stop
// carries none, so one crossing a barge-in's oww_wake on the wire would close
// the new session the controller is about to take.
func (d *DataClient) ControllerMicStop(muted bool) {
	if d.ListenState() != ListenStream {
		if d.TurnStreamActive() {
			d.StopMic()
			if !muted {
				d.StartMic(false)
			}
		}
		return
	}
	d.StopMic()
}
