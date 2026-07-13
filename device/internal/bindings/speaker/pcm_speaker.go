//go:build server

package speaker

import (
	"fmt"
	"log"
	"os/exec"
	"sync"
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

	// stateMu guards streamActive and discarding as one unit. They used to
	// be independent atomics, but Flush's check-streamActive-then-arm and
	// EndStream's clear-both are compound transitions: a barge-in Flush
	// racing a stream's natural EndStream (control and data ride separate
	// WebSockets) could observe streamActive just before EndStream cleared
	// it and then arm discarding just after EndStream consumed it — leaving
	// discard armed with no EOS ever coming, silently swallowing the whole
	// NEXT response up to its EOS.
	stateMu sync.Mutex
	// streamActive tracks whether a 0x02 stream is mid-flight (set by
	// PumpPeriod, cleared by EndStream — both on the WS read goroutine).
	// Read by Flush to decide whether to arm discarding.
	streamActive bool
	// discarding, when set, makes PumpPeriod drop incoming periods until
	// the stream's 0x03 EOS arrives. Armed by Flush (barge-in) when a
	// stream is mid-flight: draining audioCh alone is not enough, because
	// the rest of the cancelled stream is typically already in flight in
	// the TCP buffers of both ends — the WS reader would refill the channel
	// straight after the drain and playback would carry on after a ~1.3s
	// skip (observed 2026-07-08: barge-in cut the LED but the TTS kept
	// talking, and the interrupting turn transcribed the device's own
	// voice). The controller always terminates a stream with 0x03, on the
	// cancel path included, so discard-until-EOS consumes exactly the
	// remainder of the cancelled stream no matter how much was buffered.
	discarding bool

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
	// Startup order matters for the audible click (2026-07-10): the amp
	// must come up onto a DAC that is already clocking silence, and the
	// unmute must come last. The old order (amp on → unmute → open PCM)
	// unmuted a floating DAC and then hit it with the stream-open
	// transient — the "click" on every service start.
	exec.Command("stop", "mixer").Run()
	exec.Command("tinymix", "-D", "0", "61", "0", "0").Run() // mute before touching amp or stream

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

	time.Sleep(100 * time.Millisecond)                            // silence reaches the DAC (~2 periods)
	exec.Command("tinymix", "-D", "0", "5", "On").Run()           // enable amp onto a clocked, silent DAC
	time.Sleep(50 * time.Millisecond)                             // let amp settle
	exec.Command("tinymix", "-D", "0", "61", "100", "100").Run()  // unmute

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
	var underruns uint64 // loop-local: only this goroutine drains audioCh
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
					// Natural end of stream (0x03 received), or a barge-in
					// flush (Flush sets eosPending so its drain isn't
					// miscounted as an underrun).
					log.Printf("[speaker] stream complete — returning to silence")
				} else {
					// Mid-stream drain: the WS sender fell behind real-time
					// playback and a silence gap is being injected — the
					// audible stutter on weak WiFi links. One count per
					// drain event (not per silence period); rate-limited so
					// a chronically starved link can't flood the log.
					underruns++
					if underruns == 1 || underruns%16 == 0 {
						log.Printf("[speaker] UNDERRUN: audio channel drained mid-stream — injecting silence (underruns=%d)", underruns)
					}
				}
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
	p.stateMu.Lock()
	if p.discarding {
		// Flushed stream — swallow the network-buffered remainder without
		// queueing it (see the discarding field for why draining audioCh
		// alone can't do this).
		p.stateMu.Unlock()
		return nil
	}
	p.streamActive = true
	p.stateMu.Unlock()
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
	p.stateMu.Lock()
	p.streamActive = false
	wasDiscarding := p.discarding
	p.discarding = false
	p.stateMu.Unlock()
	if wasDiscarding {
		log.Printf("[speaker] flush complete — EOS reached, discard disarmed")
		return
	}
	p.eosPending.Store(true)
}

// Flush cuts a playing stream immediately (barge-in). Two parts:
//   1. Drain audioCh — kills up to ~1.3s already queued on-device.
//   2. Arm discarding (if a stream is mid-flight) — PumpPeriod then drops
//      every subsequent period of this stream until its 0x03 EOS arrives.
//      Necessary because the controller writes the whole response into the
//      WebSocket ahead of playback: at barge time the rest of the stream
//      is already in TCP buffers and would refill audioCh right after the
//      drain (the pre-2026-07-08 version drained only, and playback
//      resumed after a ~1.3s skip). The controller sends 0x03 on the
//      cancel path too, so the discard always terminates.
//
// Up to PeriodCount ALSA periods (~170ms) already handed to the hardware
// still play — cutting those needs a stream restart, which costs more in
// click/pop than it saves. The streamActive check keeps a flush that races
// a stream's natural end (control and data travel on separate WebSockets)
// from arming discard against the *next* stream's audio.
func (p *PcmSpeaker) Flush() {
	p.stateMu.Lock()
	armed := p.streamActive
	if armed {
		p.discarding = true
	}
	p.stateMu.Unlock()
	n := 0
	for {
		select {
		case <-p.audioCh:
			n++
		default:
			if n > 0 || armed {
				p.eosPending.Store(true)
				log.Printf("[speaker] flushed %d buffered periods (barge-in), discard-until-EOS armed=%v", n, armed)
			}
			return
		}
	}
}

// Close shuts the speaker down in the reverse of Init's bring-up: mute,
// amp off, then tear the stream down. Muting first makes the PCM-close
// transient inaudible, and leaving the amp off means an idle DAC can't
// hiss while the server isn't running (OTA gaps, crashes, service stop —
// the "speaker noise between OTAs"). start_server.sh repeats the mute +
// amp-off after every server exit as a belt-and-braces for paths where
// this never runs (SIGKILL, panic).
func (p *PcmSpeaker) Close() {
	exec.Command("tinymix", "-D", "0", "61", "0", "0").Run() // mute
	exec.Command("tinymix", "-D", "0", "5", "Off").Run()     // amp off
	close(p.stopCh)
	p.session.Close()
	log.Println("PcmSpeaker closed — output muted, amp off")
}
