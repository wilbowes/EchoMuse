package speaker

type Speaker interface {
	Init() error
	PumpPeriod(data []byte) error
	// EndStream marks the current audio stream as complete, so the driver
	// can distinguish "channel drained because playback finished" from a
	// mid-stream underrun.
	EndStream()
	// Flush discards queued-but-unplayed audio immediately (barge-in).
	Flush()
	// NoteDataLinkGap records that the device's data connection to the
	// controller has just been re-established. Anything still in flight crossed
	// a gap in the wire, so its per-stream arrival timings would measure that
	// outage rather than the playback (#307). The data client owns this fact;
	// the driver has no way to know it, and guessing from the clock would
	// corrupt exactly the rows the flag exists to rescue.
	NoteDataLinkGap()

	// ── music plane ───────────────────────────────────────────────────────
	// A second, independent stream, mixed with the voice plane at the ALSA
	// write. It exists so a voice turn can DUCK music rather than pausing
	// it: the controller runs its music feed ~4s ahead of realtime, so the
	// next four seconds are already on the device when a wake word fires,
	// and audio that has left the controller cannot be ducked there.
	PumpMusic(data []byte) error
	EndMusicStream()
	// FlushMusic is for the user genuinely stopping or pausing. A voice turn
	// must duck instead — flushing throws away the buffered audio that makes
	// ducking instant, and on a non-seekable stream it cannot be recovered.
	FlushMusic()
	// SetDuck sets music attenuation in dB while voice plays (0 = none).
	SetDuck(db float64)

	Close()
}
