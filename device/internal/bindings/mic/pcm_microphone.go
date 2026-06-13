//go:build server

package mic

import (
	"context"
	"errors"
	"log"
	"os/exec"
	"sync"

	pkgmic "github.com/wilbowes/EchoMuse/pkg/mic"
	"github.com/Binozo/GoTinyAlsa/pkg/pcm"
	"github.com/Binozo/GoTinyAlsa/pkg/tinyalsa"
)

const cardNr = 0
const deviceNr = 24

// PcmMicrophone opens the ALSA device once and fans out to multiple subscribers.
// Callers register via Listen(); each gets their own buffered channel.
type PcmMicrophone struct {
	device *tinyalsa.AlsaDevice
	mu     sync.Mutex
	subs   []chan []byte
}

// NewMicrophone returns the pre-configured microphone alsa device and starts
// the permanent ALSA read loop.
func NewMicrophone() (*PcmMicrophone, error) {
	device := tinyalsa.NewDevice(cardNr, deviceNr, pcm.Config{
		Channels:    9,
		SampleRate:  16000,
		PeriodSize:  512,
		PeriodCount: 5,
		Format:      tinyalsa.PCM_FORMAT_S24_3LE,
	})
	m := &PcmMicrophone{
		device: &device,
	}
	if err := m.Init(); err != nil {
		return nil, err
	}
	return m, nil
}

// Init stops the mixer service (required to release the ALSA capture device)
// then starts the permanent background ALSA read loop.
func (p *PcmMicrophone) Init() error {
	cmd := exec.Command("stop", "mixer")
	if err := cmd.Run(); err != nil {
		log.Printf("mic: stop mixer: %v (continuing)", err)
	}
	go p.readLoop()
	return nil
}

// readLoop opens the ALSA device and reads periods forever, fanning each
// period out to all current subscribers. Runs for the lifetime of the process.
// When the stream ends (ALSA error), all subscriber channels are closed so
// callers unblock and can detect the death rather than hanging on empty channels.
func (p *PcmMicrophone) readLoop() {
	stream := make(chan []byte, 16)

	go func() {
		if err := p.device.GetAudioStream(p.device.DeviceConfig, stream); err != nil {
			log.Printf("mic: ALSA stream error: %v", err)
		}
	}()

	for audio := range stream {
		// Copy so each subscriber gets its own slice
		buf := make([]byte, len(audio))
		copy(buf, audio)

		p.mu.Lock()
		for _, ch := range p.subs {
			select {
			case ch <- buf:
			default:
				// Subscriber too slow — drop this period rather than block
			}
		}
		p.mu.Unlock()
	}

	// Stream ended — close all subscriber channels so callers see EOF rather
	// than blocking on a channel that will never receive again.
	log.Printf("mic: ALSA stream closed — notifying %d subscribers", len(p.subs))
	p.mu.Lock()
	for _, ch := range p.subs {
		close(ch)
	}
	p.subs = nil
	p.mu.Unlock()
}

// subscribe registers a new subscriber and returns its channel.
func (p *PcmMicrophone) Subscribe() chan []byte {
	ch := make(chan []byte, 32)
	p.mu.Lock()
	p.subs = append(p.subs, ch)
	p.mu.Unlock()
	return ch
}

// Unsubscribe removes a subscriber channel. Safe to call even if readLoop has
// already closed the channel (e.g. after an ALSA stream error).
func (p *PcmMicrophone) Unsubscribe(ch chan []byte) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for i, s := range p.subs {
		if s == ch {
			p.subs = append(p.subs[:i], p.subs[i+1:]...)
			// Only close if readLoop hasn't already closed it (subs==nil means
			// readLoop ran the close-all path and cleared the slice).
			// We detect this by the channel still being in the slice — if we
			// found it, readLoop hasn't closed it yet.
			close(ch)
			return
		}
	}
	// Not found — readLoop already closed and cleared it. Nothing to do.
}

// Listen subscribes to the permanent mic stream and calls callback for each
// period until ctx is cancelled. Satisfies the pkgmic.Microphone interface.
func (p *PcmMicrophone) Listen(callback pkgmic.AudioCallback, ctx context.Context) error {
	if callback == nil {
		return errors.New("callback can't be nil")
	}
	ch := p.Subscribe()
	defer p.Unsubscribe(ch)

	for {
		select {
		case <-ctx.Done():
			return nil
		case audio, ok := <-ch:
			if !ok {
				return nil
			}
			callback(audio)
		}
	}
}
