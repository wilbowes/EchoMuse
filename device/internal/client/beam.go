package client

// beamEngine is what the mic pipeline (data.go) needs from a channel-
// selection stage. biscuit's *beamformer.Beamformer implements it directly
// (7-mic perimeter array, energy-onset steering — see internal/beamformer's
// package comment). crown's mic is a different shape entirely: 6 channels,
// 4 real capsules across two dies with no perimeter geometry to steer, so
// MVP stays single-channel/no-beamforming — it gets a plain channel
// extractor instead (beam_crown.go).
//
// This interface exists so data.go's mic pipeline — the most delicate hot
// loop in the binary, with its own concurrency comments about Lock/Process
// overlap — needs no per-board branching of its own. Only which concrete
// engine newBeam() constructs differs; the pipeline calls the same four
// methods either way.
type beamEngine interface {
	// Lock/Unlock mark a voice turn's start/end. A no-op engine (nothing to
	// steer) simply never changes what Process returns.
	Lock(enabled bool)
	Unlock()
	// Process extracts one mono S16LE period from a raw multi-channel
	// S24_3LE period, applying gain (linear, pre-computed from micGainDb).
	// angle is the estimated source direction in degrees, or -1 when
	// unlocked/not applicable — data.go only acts on it when it is >= 0, so
	// an engine with no direction concept can return -1 unconditionally.
	Process(raw []byte, steerAngle float64, gain float64) (mono []byte, angle float64)
	// ClippedSamples is the running count of samples clamped by gain.
	ClippedSamples() uint64
}
