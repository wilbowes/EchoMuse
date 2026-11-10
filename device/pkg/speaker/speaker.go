package speaker

import "time"

type Speaker interface {
	Init() error
	PumpPeriod(data []byte) error
	// EndStream marks the current audio stream as complete, so the driver
	// can distinguish "channel drained because playback finished" from a
	// mid-stream underrun.
	EndStream()
	// Flush discards queued-but-unplayed audio immediately (barge-in).
	Flush()

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

	// AbandonStreams ends any stream still open when the data link that
	// carried it closed at linkLost: its EOS cannot arrive on the next link.
	AbandonStreams(linkLost time.Time)

	Close()
}
