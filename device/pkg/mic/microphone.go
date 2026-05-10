package mic

import (
	"context"
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