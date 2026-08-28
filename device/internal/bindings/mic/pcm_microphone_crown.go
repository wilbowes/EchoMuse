//go:build crown

package mic

import (
	"context"
	"errors"
	"io"
	"log"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/alsa"
	pkgmic "github.com/wilbowes/EchoMuse/pkg/mic"
)

// crown: mic is card0,device22 (2x TLV320AIC3101, 6ch/16kHz/S24_3LE) —
// proven on real hardware 2026-08-26 (device/tools/capture_mics), driver
// range confirmed by HW_REFINE to match checkers exactly. See
// docs/echo-show-8-hardware-map.md. Only 4 of the 6 channels carry a live
// mic (ch4/ch5 measured as idle TDM slots, not an AEC reference); the
// beamformer/channel selection above this package decides what to do with
// that, this binding just delivers all 6.
const cardNr = 0
const deviceNr = 22
const channels = 6

// periodFrames/periods: EXACTLY capture_mics' proven config (512/5), not a
// margin choice. HW_REFINE reports 257..2570 frames / 1..10 periods per
// dimension independently, which does not mean every combination inside
// those ranges is jointly achievable — internal/alsa.Open() sets each
// param via setExact (fixed value), not "nearest", so a combination the
// driver can't hit fails HW_PARAMS outright rather than rounding. Tried
// 512/8 first (checkers' post-load-testing margin, carried over as a
// starting point) and it failed on real hardware 2026-08-26 with
// "HW_PARAMS ... invalid argument" despite every individual field being
// in-range — the buffer_size=512*8=4096 combination itself isn't valid
// here even though the periods and period-size intervals overlap it.
// 512/5 (2560 frames, capture_mics' -channels 6 5 invocation, the one
// combination actually opened on this driver) works. Margin under load is
// therefore still unmeasured, same as before, but the number is real now
// rather than borrowed from a different board's driver.
const periodFrames = 512
const periods = 5

// PcmMicrophone is crown's capture binding: same fan-out-to-subscribers shape
// as biscuit's (pcm_microphone.go), over internal/alsa's blocking Read
// instead of GoTinyAlsa's channel-based GetAudioStream — this driver has
// never been run against GoTinyAlsa/tinyalsa, and internal/alsa is already
// proven against it via HW_REFINE and capture_mics.
type PcmMicrophone struct {
	pcm  *alsa.PCM
	mu   sync.Mutex
	subs []chan []byte
}

func NewMicrophone() (*PcmMicrophone, error) {
	m := &PcmMicrophone{}
	if err := m.Init(); err != nil {
		return nil, err
	}
	return m, nil
}

// Init opens the capture PCM and starts the permanent read loop.
//
// No init service to stop first: `stop mixer` on this platform returned
// "exit status 1" against a real unit — there is no such service — and per
// TestNeverStopAudioserver's reasoning (device/internal/profile is gone, but
// the finding stands, see docs/echo-show-8-hardware-map.md), nothing in the
// audioserver family may ever be added here.
func (m *PcmMicrophone) Init() error {
	pcm, err := alsa.Open(alsa.Config{
		Card: cardNr, Device: deviceNr, Playback: false,
		Channels: channels, Format: alsa.FormatS24_3LE, Rate: 16000,
		PeriodSize: periodFrames, Periods: periods,
	})
	if err != nil {
		return err
	}
	m.pcm = pcm
	// Missing until review 2026-08-27: the speaker binding logs
	// "PcmSpeaker (crown) initialised" on successful Init, and this one
	// never had an equivalent — so a device log showing everything else
	// starting cleanly (speaker, LEDs, control connection) but nothing here
	// is indistinguishable from a device where this line was never added at
	// all. Both readings used to be the same log; they no longer are.
	log.Printf("PcmMicrophone (crown) initialised — capture card=%d device=%d channels=%d",
		cardNr, deviceNr, channels)
	go m.readLoop()
	return nil
}

// readLoop reads whole-ring batches forever and fans each one out to every
// current subscriber, mirroring biscuit's readLoop (pcm_microphone.go) —
// same stall/clock telemetry, same copy-per-subscriber, same close-all-on-
// death behaviour — over a plain blocking Read instead of a stream channel.
//
// Reads periodFrames*periods (one full buffer, ~160ms) per call rather than
// one 512-frame/32ms period at a time. Measured on real hardware
// 2026-08-26: reading single periods stalled on nearly every call (66-84ms
// between 32ms batches, i.e. losing ~35-50ms every ~70ms) — the same reason
// CLAUDE.md documents for biscuit's GoTinyAlsa binding reading its whole
// ALSA buffer per chunk rather than one period at a time. Same SoC family
// (MT8163), evidently the same scheduling-granularity floor.
func (m *PcmMicrophone) readLoop() {
	batchFrames := periodFrames * periods
	batchBytes := batchFrames * channels * 3 // S24_3LE
	buf := make([]byte, batchBytes)

	rate := int64(16000)
	var (
		firstArrival time.Time
		lastArrival  time.Time
		lastReport   time.Time
		framesTotal  int64
		stalls       uint64
		subDrops     uint64
	)

	for {
		// io.ReadFull, not a single Read: alsa.PCM.Read is a thin wrapper
		// over the read(2) syscall and can legitimately return fewer bytes
		// than asked for one buffer this size (~160ms/2560 frames) without
		// erroring — a single Read() call took the short read as-is here
		// first, and downstream code (AEC in particular, which requires
		// exact multiples of its 1024-byte speex frame) got a batch that
		// didn't line up, bypassed itself, and something about the
		// resulting misalignment destabilised the whole mic goroutine
		// (measured on real hardware 2026-08-26: repeated unexplained
		// stream stop/restart cycles disappeared once this loop was
		// fixed). n==0 with no error is EPIPE recovery re-arming the
		// stream (alsa.PCM.Read's own doc) — ReadFull just calls it again.
		n, err := io.ReadFull(m.pcm, buf)
		if err != nil {
			log.Printf("mic: ALSA read error: %v", err)
			break
		}

		now := time.Now()
		frames := int64(n / (channels * 3))
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

		out := make([]byte, n)
		copy(out, buf[:n])

		m.mu.Lock()
		for _, ch := range m.subs {
			select {
			case ch <- out:
			default:
				subDrops++
				if subDrops == 1 || subDrops%64 == 0 {
					log.Printf("[mic] subscriber channel full — batch dropped (sub_drops=%d)", subDrops)
				}
			}
		}
		m.mu.Unlock()
	}

	m.mu.Lock()
	log.Printf("mic: ALSA stream closed — notifying %d subscribers", len(m.subs))
	for _, ch := range m.subs {
		close(ch)
	}
	m.subs = nil
	m.mu.Unlock()
}

func (m *PcmMicrophone) Subscribe() chan []byte {
	ch := make(chan []byte, 32)
	m.mu.Lock()
	m.subs = append(m.subs, ch)
	m.mu.Unlock()
	return ch
}

func (m *PcmMicrophone) Unsubscribe(ch chan []byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for i, s := range m.subs {
		if s == ch {
			m.subs = append(m.subs[:i], m.subs[i+1:]...)
			close(ch)
			return
		}
	}
}

func (m *PcmMicrophone) Listen(callback pkgmic.AudioCallback, ctx context.Context) error {
	if callback == nil {
		return errors.New("callback can't be nil")
	}
	ch := m.Subscribe()
	defer m.Unsubscribe(ch)

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
