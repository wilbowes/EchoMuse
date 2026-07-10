//go:build server

package mic

import (
	"context"
	"errors"
	"log"
	"os/exec"
	"sync"
	"time"

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
//
// Capture-loss telemetry (2026-07-10): the ALSA ring is only PeriodSize ×
// PeriodCount = 160ms deep, so any stall of this chain longer than that
// loses whole batches at the hardware with no error surfaced anywhere —
// discovered via the AEC reference governor tripping every ~20s on backlogs
// of exactly N×2560 samples. Two measurements below: per-batch arrival gaps
// (a gap ≫ the batch duration is an overrun in progress) and a ~1/min
// audio-vs-wall-clock ledger (steady deficit growth = chronic loss; it also
// distinguishes overruns from a clock-rate mismatch, which would grow the
// deficit smoothly rather than in stall-sized steps).
func (p *PcmMicrophone) readLoop() {
	stream := make(chan []byte, 16)

	go func() {
		if err := p.device.GetAudioStream(p.device.DeviceConfig, stream); err != nil {
			log.Printf("mic: ALSA stream error: %v", err)
		}
	}()

	rate := int64(p.device.DeviceConfig.SampleRate)
	bytesPerFrame := p.device.DeviceConfig.Channels * 3 // S24_3LE
	var (
		firstArrival time.Time
		lastArrival  time.Time
		lastReport   time.Time
		framesTotal  int64
		stalls       uint64
		subDrops     uint64
	)

	for audio := range stream {
		now := time.Now()
		frames := int64(len(audio) / bytesPerFrame)
		batchDur := time.Duration(frames) * time.Second / time.Duration(rate)
		if firstArrival.IsZero() {
			firstArrival, lastReport = now, now
		} else if gap := now.Sub(lastArrival); gap > 2*batchDur {
			stalls++
			log.Printf("[mic] capture stall: %dms between %dms batches — ~%dms lost to ALSA overrun (stalls=%d)",
				gap.Milliseconds(), batchDur.Milliseconds(),
				(gap - batchDur).Milliseconds(), stalls)
		}
		lastArrival = now
		framesTotal += frames
		if now.Sub(lastReport) >= time.Minute {
			wall := now.Sub(firstArrival)
			audioDur := time.Duration(framesTotal) * time.Second / time.Duration(rate)
			log.Printf("[mic] clock: %.1fs audio over %.1fs wall (deficit %+dms, stalls=%d, sub_drops=%d)",
				audioDur.Seconds(), wall.Seconds(), (wall - audioDur).Milliseconds(), stalls, subDrops)
			lastReport = now
		}

		// Copy so each subscriber gets its own slice
		buf := make([]byte, len(audio))
		copy(buf, audio)

		p.mu.Lock()
		for _, ch := range p.subs {
			select {
			case ch <- buf:
			default:
				// Subscriber too slow — drop this period rather than block
				subDrops++
				if subDrops == 1 || subDrops%64 == 0 {
					log.Printf("[mic] subscriber channel full — batch dropped (sub_drops=%d)", subDrops)
				}
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
