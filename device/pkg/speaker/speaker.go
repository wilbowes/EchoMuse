package speaker

type Speaker interface {
	Init() error
	PumpPeriod(data []byte) error
	// EndStream marks the current audio stream as complete, so the driver
	// can distinguish "channel drained because playback finished" from a
	// mid-stream underrun.
	EndStream()
	Close()
}
