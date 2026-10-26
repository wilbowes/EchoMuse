//go:build server

package mic

import (
	"context"
	"errors"
	"log"
	"os/exec"
	"sync"
	"time"

	"github.com/Binozo/GoTinyAlsa/pkg/pcm"
	"github.com/Binozo/GoTinyAlsa/pkg/tinyalsa"
	"github.com/wilbowes/EchoMuse/internal/bindings/codec"
	"github.com/wilbowes/EchoMuse/pkg/board"
	pkgmic "github.com/wilbowes/EchoMuse/pkg/mic"
)

// rawTap receives every raw capture batch, and is nil in release builds.
// Only rawtap_bench.go sets it (build tag bench): it records the mics to
// disk, which release firmware must be unable to do.
var rawTap func([]byte)

// PcmMicrophone opens the ALSA device once and fans out to multiple subscribers.
// Callers register via Listen(); each gets their own buffered channel.
type PcmMicrophone struct {
	device        *tinyalsa.AlsaDevice
	bytesPerFrame int
	mu            sync.Mutex
	subs          []chan []byte
}

// NewMicrophone returns the pre-configured microphone alsa device and starts
// the permanent ALSA read loop.
func NewMicrophone() (*PcmMicrophone, error) {
	// The capture PCM is found by name (pkg/board), never by device number.
	layout := board.CurrentLayout()
	capture := layout.Capture
	if capture == nil {
		return nil, errors.New("no microphone this firmware can read on this board")
	}
	cfg := pcm.Config{
		Channels:    layout.Mics.Channels,
		SampleRate:  16000,
		PeriodSize:  512,
		PeriodCount: 5,
		Format:      tinyalsa.PCM_FORMAT_S24_3LE,
	}
	if layout.Mics.Format == board.S32LE {
		cfg.Format = tinyalsa.PCM_FORMAT_S32_LE
	}
	device := tinyalsa.NewDevice(capture.Card, capture.Device, cfg)
	m := &PcmMicrophone{
		device:        &device,
		bytesPerFrame: layout.Mics.Channels * layout.Mics.Format.Bytes(),
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
	// Route the differential mic inputs into the ADCs before opening the PCM.
	// Without this the ADCs are powered down and capture returns the I2S bus's
	// own noise floor — with a perfectly healthy ALSA clock, which is what
	// makes it so hard to see. See the codec package.
	codec.EnsureRoutes()
	if err := openLoopback(board.CurrentLayout()); err != nil {
		return err
	}
	go p.readLoop()
	return nil
}

// openLoopback opens the board's playback loopback, if it states one, and
// returns once it is delivering audio, so that it is running before the
// microphones open: the Dot 3's amplifier loses its clock until reboot the
// other way round (41C8, 2026-10-24). It is read and dropped for as long as
// the process runs; nothing uses it yet.
//
// It is retried for a few seconds because "stop mixer" returns before
// Amazon's mixer has let the PCM go, and the first open then fails at once
// (41C8, 2026-10-25).
func openLoopback(l *board.Layout) error {
	if l.Loopback == nil {
		return nil
	}
	for try := 1; ; try++ {
		stream, err := startLoopback(l)
		if err == nil {
			log.Printf("mic: loopback open at card %d device %d, before the microphones (try %d)",
				l.Loopback.Card, l.Loopback.Device, try)
			go func() {
				for range stream {
				}
			}()
			return nil
		}
		if try == loopbackTries {
			return err
		}
		time.Sleep(500 * time.Millisecond)
	}
}

const loopbackTries = 8

// startLoopback opens the loopback once and waits for its first audio.
func startLoopback(l *board.Layout) (chan []byte, error) {
	dev := tinyalsa.NewDevice(l.Loopback.Card, l.Loopback.Device, pcm.Config{
		Channels:    l.LoopbackChs,
		SampleRate:  l.LoopbackRate,
		PeriodSize:  768,
		PeriodCount: 10,
		Format:      tinyalsa.PCM_FORMAT_S16_LE,
	})
	stream := make(chan []byte, 4)
	go func() {
		if err := dev.GetAudioStream(dev.DeviceConfig, stream); err != nil {
			log.Printf("mic: loopback stream error: %v", err)
		}
		close(stream)
	}()
	select {
	case _, ok := <-stream:
		if !ok {
			return nil, errors.New("the loopback that must open before the microphones did not start")
		}
	case <-time.After(2 * time.Second):
		return nil, errors.New("the loopback that must open before the microphones delivered nothing in 2s")
	}
	return stream, nil
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
// audio-vs-wall-clock ledger.
//
// THE LEDGER'S SIGN IS ITS WHOLE MEANING, and it reads both ways. Positive
// (wall ahead of audio) is audio the pipeline never got — overruns, the case
// this was built for. Negative is the capture clock running FAST, which is
// the ordinary state of this hardware and not a fault.
//
// Measured on SPJ over 11.8h, 2026-09-02: skew went -154ms to -14809ms, a
// steady **+345ppm** of the ALSA sample clock against CLOCK_MONOTONIC. The
// starting -154ms is just this loop counting the first batch's 160ms while
// wall starts at its arrival; everything after is the rate mismatch.
//
// It arrives in 160ms STEPS, roughly one every 7.7 minutes, and the earlier
// version of this comment said a rate mismatch would grow the deficit
// "smoothly rather than in stall-sized steps" — it does not, because
// delivery is quantised to whole 160ms batches, so the surplus banks in the
// ALSA ring until it is a whole batch and then lands at once. 160ms ÷
// 345ppm = 464s predicted against ~462s observed, which is what identifies
// it. So step-shaped growth does NOT distinguish overruns from drift; the
// SIGN does, and a device that is losing audio also raises stalls.
//
// Consequence for whoever reads a long uptime: seconds of accumulated
// negative skew are expected and mean nothing is wrong.
func (p *PcmMicrophone) readLoop() {
	stream := make(chan []byte, 16)

	go func() {
		if err := p.device.GetAudioStream(p.device.DeviceConfig, stream); err != nil {
			log.Printf("mic: ALSA stream error: %v", err)
		}
	}()

	rate := int64(p.device.DeviceConfig.SampleRate)
	bytesPerFrame := p.bytesPerFrame
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
			// Name the direction rather than leaving a signed number to be
			// read as loss either way — see readLoop's ledger note.
			skew := (wall - audioDur).Milliseconds()
			sense := "lost"
			if skew < 0 {
				sense = "capture fast"
			}
			log.Printf("[mic] clock: %.1fs audio over %.1fs wall (skew %+dms %s, stalls=%d, sub_drops=%d)",
				audioDur.Seconds(), wall.Seconds(), skew, sense, stalls, subDrops)
			lastReport = now
		}

		// GetAudioStream hands over a fresh slice per read (GoTinyAlsa #1),
		// so this can be passed on as-is. Copying here was too late: the
		// library reused one buffer, and a batch still queued in stream was
		// overwritten by the next read — repeated or torn audio (#607).
		buf := audio
		if rawTap != nil {
			rawTap(buf)
		}

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
	p.mu.Lock()
	log.Printf("mic: ALSA stream closed — notifying %d subscribers", len(p.subs))
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
