//go:build server

package speaker

import (
	"fmt"
	"log"
	"os/exec"
	"sync/atomic"
	"time"

	"github.com/Binozo/GoTinyAlsa/pkg/pcm"
	"github.com/Binozo/GoTinyAlsa/pkg/tinyalsa"
)

const cardNr      = 0
const deviceNr    = 23
const periodSize  = 2048
const periodBytes = periodSize * 2 * 2 // 2 channels * 2 bytes = 8192

// audioCh depth — deep enough that the WS sender stays well ahead of the
// silence loop on any realistic LAN jitter. At 4 the channel drained
// momentarily mid-stream, causing the default silence case to fire and
// inject a 42ms silence gap (audible stutter). 32 periods = ~1.3s of
// headroom; the WS reader would need to stall for over a second before
// the channel empties mid-stream.
const audioChanDepth = 32

var silencePeriod = make([]byte, periodBytes)

type PcmSpeaker struct {
	session  *tinyalsa.AudioSession
	audioCh  chan []byte
	stopCh   chan struct{}
	// deadCh is closed by silenceLoop on any exit so PumpPeriod can return
	// an error rather than block indefinitely waiting for a dead consumer.
	deadCh   chan struct{}
	// eosPending is set by EndStream (WS reader received 0x03) and consumed
	// by silenceLoop when audioCh drains, so a drain at natural end of
	// stream isn't misreported as an underrun.
	eosPending atomic.Bool

	// echoTap, when non-nil, receives every period pumped to ALSA — real
	// audio and silence alike — so an AEC reference stream advances in
	// lockstep with the playback clock. Fixed at construction (silenceLoop
	// starts inside New, so it can't be set later without a race). Must be
	// fast and non-blocking: it runs on the ALSA pump goroutine.
	echoTap func([]byte)
}

func NewPcmSpeaker(echoTap func([]byte)) (*PcmSpeaker, error) {
	s := &PcmSpeaker{
		audioCh: make(chan []byte, audioChanDepth),
		stopCh:  make(chan struct{}),
		deadCh:  make(chan struct{}),
		echoTap: echoTap,
	}
	if err := s.Init(); err != nil {
		return nil, err
	}
	return s, nil
}

func (p *PcmSpeaker) Init() error {
	exec.Command("stop", "mixer").Run()
	exec.Command("tinymix", "-D", "0", "61", "0", "0").Run()    // mute before amp enable
	exec.Command("tinymix", "-D", "0", "5", "On").Run()          // enable amp
	time.Sleep(50 * time.Millisecond)                             // let amp settle
	exec.Command("tinymix", "-D", "0", "61", "100", "100").Run() // unmute

	device := tinyalsa.NewDevice(cardNr, deviceNr, pcm.Config{
		Channels:         2,
		SampleRate:       48000,
		PeriodSize:       periodSize,
		PeriodCount:      4,
		Format:           tinyalsa.PCM_FORMAT_S16_LE,
		StartThreshold:   periodSize,
		StopThreshold:    periodSize * 4,
		SilenceThreshold: periodSize * 4,
	})

	session, err := device.NewAudioSession()
	if err != nil {
		return err
	}
	p.session = &session

	go p.silenceLoop()

	log.Println("PcmSpeaker initialised — silence stream running")
	return nil
}

// silenceLoop runs continuously, playing real audio from audioCh when available
// and silence when the channel is empty. No pause/resume needed — the select
// naturally yields to real audio. Closes deadCh on any exit so PumpPeriod
// callers unblock and receive an error rather than hanging.
//
// audioStreaming tracks whether we are mid-stream. When the channel drains
// while audioStreaming is true, that's an underrun — a silence period is
// being injected mid-content. Logged at WARNING so it shows up in server.log
// against the stutter symptom. Remove once the cause is confirmed and fixed.
func (p *PcmSpeaker) silenceLoop() {
	defer close(p.deadCh)
	audioStreaming := false
	for {
		select {
		case <-p.stopCh:
			return
		case period := <-p.audioCh:
			audioStreaming = true
			if p.echoTap != nil {
				p.echoTap(period)
			}
			if err := p.session.Pump(period); err != nil {
				log.Printf("silenceLoop: pump error: %v", err)
				return
			}
		default:
			if audioStreaming {
				if p.eosPending.Swap(false) {
					// Natural end of stream (0x03 received).
					log.Printf("[speaker] stream complete — returning to silence")
				}
				// Q5 (2026-07-07): the mid-stream underrun WARNING that
				// lived here is gone — the v2.6.5 EOS disambiguation plus
				// the deeper audioCh removed the underruns it was hunting,
				// and it stayed clean for several sessions.
				audioStreaming = false
			}
			if p.echoTap != nil {
				p.echoTap(silencePeriod)
			}
			if err := p.session.Pump(silencePeriod); err != nil {
				log.Printf("silenceLoop: silence pump error: %v", err)
				return
			}
		}
	}
}

// PumpPeriod queues one period of audio for playback. Called by the WS client
// for each incoming 0x02 binary frame. Blocks until the silence loop has
// consumed a slot (rate-limiting to ALSA speed), or returns an error if the
// silence loop has died — preventing an infinite block on a dead consumer.
func (p *PcmSpeaker) PumpPeriod(data []byte) error {
	period := make([]byte, len(data))
	copy(period, data)
	select {
	case p.audioCh <- period:
		return nil
	case <-p.deadCh:
		return fmt.Errorf("speaker: ALSA loop has died")
	}
}

// EndStream marks the in-flight stream as complete. Called by the WS client
// on the 0x03 EOS frame — always after every 0x02 period of that stream has
// already been handed to PumpPeriod (frames are processed sequentially on
// the read loop), so by the time silenceLoop drains audioCh the flag is set.
func (p *PcmSpeaker) EndStream() {
	p.eosPending.Store(true)
}

// Flush discards all queued-but-unplayed audio (barge-in: the controller
// stops sending 0x02 frames and asks for an immediate cut instead of
// letting up to ~1.4s of buffered TTS play out). Up to PeriodCount ALSA
// periods (~170ms) already handed to the hardware still play — cutting
// those needs a stream restart, which costs more in click/pop than it
// saves. eosPending is set so silenceLoop reports a clean stream end.
// Draining a channel another goroutine sends on is safe; at worst the WS
// reader enqueues one more period after we return, and the controller has
// already stopped producing them.
func (p *PcmSpeaker) Flush() {
	n := 0
	for {
		select {
		case <-p.audioCh:
			n++
		default:
			if n > 0 {
				p.eosPending.Store(true)
				log.Printf("[speaker] flushed %d buffered periods (barge-in)", n)
			}
			return
		}
	}
}

func (p *PcmSpeaker) Close() {
	close(p.stopCh)
	p.session.Close()
}
