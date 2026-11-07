package mic

import (
	"context"

	"github.com/wilbowes/EchoMuse/internal/echoref"
)

type AudioCallback func(audioData []byte)

type Microphone interface {
	Init() error
	Listen(callback AudioCallback, context context.Context) error
}

// Subscribable is implemented by mic backends that support multiple concurrent
// readers via a fan-out model (i.e. PcmMicrophone). The vadStreamHandler uses
// this to tap the permanent ALSA stream without opening a second PCM session.
type Subscribable interface {
	Subscribe() chan []byte
	Unsubscribe(ch chan []byte)
}

// EchoAligned is implemented by a microphone whose board's playback loopback
// is its own PCM rather than a channel of the microphone frame (the Dot 3).
// EchoAligner is nil on every other board. BatchIndex is a batch's position
// in the microphone stream, in samples, for as long as it is recent; batches
// are identified by the slice the fan-out delivered.
type EchoAligned interface {
	EchoAligner() *echoref.Aligner
	BatchIndex(batch []byte) (int64, bool)
}
