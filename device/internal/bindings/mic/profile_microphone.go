package mic

import (
	"context"
	"errors"
	"log"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/alsa"
	"github.com/wilbowes/EchoMuse/internal/profile"
	pkgmic "github.com/wilbowes/EchoMuse/pkg/mic"
)

// ProfileMicrophone is a profile-driven capture backend built on the
// dependency-free ALSA client, replacing the hardcoded card/device/channel
// constants of PcmMicrophone.
//
// It opens the capture PCM once and fans each period out to every subscriber,
// so the wake-word path, the AEC reference and any diagnostic tap all share a
// single ALSA stream, opening the device twice is not possible.
type ProfileMicrophone struct {
	prof *profile.Profile
	pcm  *alsa.PCM

	mu     sync.Mutex
	subs   []chan []byte
	closed bool

	stopped chan struct{}
	once    sync.Once
}

var (
	_ pkgmic.Microphone   = (*ProfileMicrophone)(nil)
	_ pkgmic.Subscribable = (*ProfileMicrophone)(nil)
)

// ErrStreamEnded is returned by Listen when the capture stream dies, so
// callers can distinguish a dead device from an idle one rather than blocking
// forever on an empty channel.
var ErrStreamEnded = errors.New("mic: capture stream ended")

// NewProfileMicrophone constructs a capture backend for the given profile.
// The caller is expected to have run profile.Prepare() first, so the mixer is
// configured and nothing else holds the PCM.
func NewProfileMicrophone(p *profile.Profile) *ProfileMicrophone {
	return &ProfileMicrophone{prof: p, stopped: make(chan struct{})}
}

// Init opens the capture device and starts the permanent read loop.
func (m *ProfileMicrophone) Init() error {
	cfg := alsa.Config{
		Card:       m.prof.Mic.Card,
		Device:     m.prof.Mic.Device,
		Playback:   false,
		Channels:   m.prof.Mic.Channels,
		Format:     m.prof.Mic.Format,
		Rate:       m.prof.Mic.SampleRate,
		PeriodSize: m.prof.Mic.PeriodSize,
		Periods:    m.prof.Mic.Periods,
	}
	pcm, err := alsa.Open(cfg)
	if err != nil {
		return err
	}
	m.pcm = pcm
	log.Printf("mic: %s card %d device %d, %d ch %v @ %d Hz, period %d x %d (buffer %d frames)",
		m.prof.Name, cfg.Card, cfg.Device, cfg.Channels, cfg.Format, cfg.Rate,
		cfg.PeriodSize, cfg.Periods, pcm.BufferFrames())
	go m.readLoop()
	return nil
}

// readLoop reads periods forever and fans each out to current subscribers.
//
// The ALSA ring is only PeriodSize x Periods frames deep, 257 x 8 (~128 ms) on
// checkers, so any stall in this chain longer than that drops whole periods at
// the hardware with nothing surfaced. The telemetry
// below measures arrival gaps and keeps an audio-vs-wall-clock ledger: a
// steady deficit means chronic loss, and stall-sized steps distinguish
// overruns from a clock-rate mismatch, which would drift smoothly instead.
func (m *ProfileMicrophone) readLoop() {
	defer m.closeSubs()

	frameBytes := m.prof.Mic.FrameBytes()
	buf := make([]byte, m.prof.Mic.PeriodSize*frameBytes)
	periodDur := time.Duration(m.prof.Mic.PeriodSize) * time.Second /
		time.Duration(m.prof.Mic.SampleRate)

	// The FPGA batches periods, so treat only gaps beyond half the ring depth
	// as suspect: past that, one more stall really does lose data.
	stallThreshold := periodDur * time.Duration(m.prof.Mic.Periods) / 2
	if min := 4 * periodDur; stallThreshold < min {
		stallThreshold = min
	}

	var (
		firstArrival time.Time
		lastArrival  time.Time
		lastReport   time.Time
		lastStallLog time.Time
		framesTotal  int64
		stalls       uint64
		subDrops     uint64
	)

	for {
		select {
		case <-m.stopped:
			return
		default:
		}

		n, err := m.pcm.Read(buf)
		if err != nil {
			log.Printf("mic: read error: %v", err)
			return
		}
		if n == 0 {
			// Recovered overrun; the period is gone.
			stalls++
			continue
		}

		now := time.Now()
		if firstArrival.IsZero() {
			firstArrival, lastReport = now, now
		} else if gap := now.Sub(lastArrival); gap > stallThreshold {
			// Only gaps well beyond normal batching are worth reporting. The
			// FPGA hands over periods in pairs, so a steady ~2x-period gap is
			// the expected arrival pattern on checkers, not a stall, logging
			// at >2x produced ~30 lines per capture of pure noise.
			stalls++
			if now.Sub(lastStallLog) > time.Minute {
				log.Printf("mic: arrival gap %v (period %v, threshold %v), possible overrun",
					gap.Round(time.Millisecond), periodDur.Round(time.Millisecond), stallThreshold)
				lastStallLog = now
			}
		}
		lastArrival = now
		framesTotal += int64(n / frameBytes)

		period := make([]byte, n)
		copy(period, buf[:n])

		m.mu.Lock()
		for _, ch := range m.subs {
			select {
			case ch <- period:
			default:
				subDrops++ // slow subscriber; never block the ALSA reader
			}
		}
		m.mu.Unlock()

		if now.Sub(lastReport) >= time.Minute {
			wall := now.Sub(firstArrival)
			audio := time.Duration(framesTotal) * time.Second /
				time.Duration(m.prof.Mic.SampleRate)
			log.Printf("mic: %v audio in %v wall (deficit %v), stalls=%d sub_drops=%d",
				audio.Round(time.Millisecond), wall.Round(time.Millisecond),
				(wall - audio).Round(time.Millisecond), stalls, subDrops)
			lastReport = now
		}
	}
}

// Listen delivers periods to cb until ctx is cancelled or the stream dies.
func (m *ProfileMicrophone) Listen(cb pkgmic.AudioCallback, ctx context.Context) error {
	ch := m.Subscribe()
	defer m.Unsubscribe(ch)

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case data, ok := <-ch:
			if !ok {
				return ErrStreamEnded
			}
			cb(data)
		}
	}
}

// Subscribe registers a new consumer of the raw interleaved stream.
func (m *ProfileMicrophone) Subscribe() chan []byte {
	ch := make(chan []byte, 16)
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed {
		close(ch)
		return ch
	}
	m.subs = append(m.subs, ch)
	return ch
}

// Unsubscribe removes a consumer and closes its channel.
func (m *ProfileMicrophone) Unsubscribe(ch chan []byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for i, c := range m.subs {
		if c == ch {
			m.subs = append(m.subs[:i], m.subs[i+1:]...)
			close(c)
			return
		}
	}
}

func (m *ProfileMicrophone) closeSubs() {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed {
		return
	}
	m.closed = true
	for _, c := range m.subs {
		close(c)
	}
	m.subs = nil
}

// Close stops the read loop and releases the device.
func (m *ProfileMicrophone) Close() error {
	m.once.Do(func() { close(m.stopped) })
	if m.pcm != nil {
		return m.pcm.Close()
	}
	return nil
}
