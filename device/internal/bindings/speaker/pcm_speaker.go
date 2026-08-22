//go:build server

package speaker

import (
	"log"
	"math"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Binozo/GoTinyAlsa/pkg/pcm"
	"github.com/Binozo/GoTinyAlsa/pkg/tinyalsa"
)

// cardNr/deviceNr live in pcmstatus.go so the host test can pin them against
// the status path — this file is ARM-only (build tag `server`).
// sampleRate is the speaker path's rate everywhere — the wire, the ALSA
// config and the output chain's filter design all assume it.
const sampleRate = 48000

const periodSize = 2048
const periodBytes = periodSize * 2 * 2 // 2 channels * 2 bytes = 8192

// The wire carries MONO 48kHz — PumpPeriod duplicates L=R before queueing.
// The stereo ALSA config is an I2S/codec-path constraint, not a wire
// requirement, and shipping two identical channels to a mono speaker
// doubled TTS bandwidth (~1.5Mbps → ~770kbps saved) for nothing — which
// matters on marginal 2.4GHz links (Lounge stutter).
const monoPeriodBytes = periodSize * 2 // 4096

// audioCh depth — the WS sender delivers at ~2× realtime, so its lead over
// playback grows ~1s per second played until it hits this cap. At 32
// (~1.3s) any WiFi stall longer than the accumulated lead drained the
// channel mid-stream (audible stutter on the far-AP device). 128 periods
// ≈ 5.5s (~1MB queued as stereo): most responses land on-device entirely
// within the first half of playback.
const audioChanDepth = 128

// primePeriods — playback holds on silence until this many periods are
// queued (or the stream's EOS has arrived, for clips shorter than the
// prime). Protects the opening seconds of playback, when the sender's
// lead is still ~zero and a single WiFi stall used to stutter. 24 periods
// ≈ 1s of audio ≈ ~0.5s added start latency at 2× realtime delivery
// (accepted trade, 2026-07-14). The controller's post-playback drain
// sleep allows for the delayed start (SPEAKER_PRIME_SECONDS).
const primePeriods = 24

var silencePeriod = make([]byte, periodBytes)

type PcmSpeaker struct {
	session *tinyalsa.AudioSession
	stopCh  chan struct{}
	// deadCh is closed by silenceLoop on any exit so a pump call can return
	// an error rather than block indefinitely waiting for a dead consumer.
	deadCh chan struct{}

	// Two independent playback planes, mixed at the ALSA write.
	//
	// voice carries TTS and announcements (0x02/0x03); music carries the
	// media player (0x04/0x05). They are separate so a voice turn can duck
	// the music instead of pausing it: the music feed runs LEAD_S=4s ahead
	// of realtime, so by the time a wake word fires the next four seconds
	// are already in this buffer, and audio that has left the controller
	// cannot be ducked by the controller. Holding both here means the music
	// keeps its full ~5.5s of link-stall protection AND the duck is instant.
	//
	// All the per-stream machinery — prime gate, discard-until-EOS,
	// underrun accounting, delivery instrumentation — lives in audioStream,
	// so both planes get the behaviour that was worked out on the single
	// one rather than a simplified copy.
	voice *audioStream
	music *audioStream

	// duckTarget is the gain applied to MUSIC while it plays under a voice
	// turn, Q15. Written from the control plane (SetDuck) and read by the
	// ALSA goroutine every period, hence atomic; the ramp toward it lives in
	// the Mixer, which is single-consumer and needs no synchronisation.
	duckTarget atomic.Int32
	mixer      Mixer

	// echoTap, when non-nil, receives every period pumped to ALSA — real
	// audio and silence alike — so an AEC reference stream advances in
	// lockstep with the playback clock. Fixed at construction (silenceLoop
	// starts inside New, so it can't be set later without a race). Must be
	// fast and non-blocking: it runs on the ALSA pump goroutine.
	echoTap func([]byte)

	// levelTap, when non-nil, receives the RMS level (0..1, normalized
	// int16 full-scale) of every period as it is pumped to ALSA — real
	// audio carries its measured level, silence reports 0. Drives the
	// energy-reactive LED ring ("meter" led_anim pattern): tapping at the
	// ALSA write means the throb tracks what is audible right now, not
	// what PumpPeriod queued ~5.5s of buffer ago. Fixed at construction
	// like echoTap (silenceLoop races any later setter) and must be fast
	// and non-blocking: it runs on the ALSA pump goroutine.
	levelTap func(rms float64)

	// statsCb, when set, receives one StreamStats once per completed
	// stream — at EOS consume in silenceLoop, flush included. Unlike echoTap
	// it fires at most once per response, so a setter (OnStreamStats) is
	// fine: silenceLoop reads it under statsMu only on that cold path.
	statsMu sync.Mutex
	statsCb func(StreamStats)

	// oc is the output chain — EQ, bass guard and limiter applied to the
	// MIXED period on its way to ALSA. See outputchain.go.
	oc chainState

	// cue holds any one-shot the device is playing itself — today just the
	// wake confirmation (#120). See cue.go.
	cue cueState
}

// OnStreamStats registers a per-stream stats callback, reported once when a
// stream reaches its EOS. Invoked on its own goroutine so a slow consumer
// (network send) can never stall the ALSA pump. Safe to call any time
// after New.
func (p *PcmSpeaker) OnStreamStats(cb func(StreamStats)) {
	p.statsMu.Lock()
	p.statsCb = cb
	p.statsMu.Unlock()
}

func NewPcmSpeaker(echoTap func([]byte), levelTap func(rms float64)) (*PcmSpeaker, error) {
	s := &PcmSpeaker{
		stopCh:   make(chan struct{}),
		deadCh:   make(chan struct{}),
		echoTap:  echoTap,
		levelTap: levelTap,
	}
	s.voice = newAudioStream(audioChanDepth, s.deadCh)
	s.music = newAudioStream(audioChanDepth, s.deadCh)
	s.duckTarget.Store(unityGain)
	s.mixer.SetGainImmediate(unityGain)
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
	// Android's media stack takes the speaker for itself when a headphone
	// plug is present at boot, and ALSA parks a blocking open behind it with
	// no timeout — stranding the whole device, since everything else in
	// main() is initialised after the speaker (issue #80). Same stock-service
	// takeover as `stop mixer` above and `stop smarthomewifid` in main: on a
	// device where EchoMuse drives the codec directly, mediaserver has no
	// work to do and is only ever in the way.
	exec.Command("stop", "media").Run()
	waitForFreePcm(cardNr, deviceNr, pcmFreeTimeout)
	exec.Command("tinymix", "-D", "0", "61", "0", "0").Run() // mute before touching amp or stream

	device := tinyalsa.NewDevice(cardNr, deviceNr, pcm.Config{
		Channels:         2,
		SampleRate:       sampleRate,
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

	time.Sleep(100 * time.Millisecond)                           // silence reaches the DAC (~2 periods)
	exec.Command("tinymix", "-D", "0", "5", "On").Run()          // enable amp onto a clocked, silent DAC
	time.Sleep(50 * time.Millisecond)                            // let amp settle
	exec.Command("tinymix", "-D", "0", "61", "100", "100").Run() // unmute

	log.Println("PcmSpeaker initialised — silence stream running")
	return nil
}

// pcmFreeTimeout bounds the wait for another process to release the speaker.
//
// Generous, because the wait is the good outcome: releasing takes Android well
// under a second once `stop media` lands, and a device that waits ten seconds
// and then works is enormously better than one that gives up early. It is a
// backstop against a holder that never lets go, not a tuning parameter.
const pcmFreeTimeout = 10 * time.Second

// waitForFreePcm blocks until the playback substream is released, or the
// timeout expires.
//
// On timeout it RETURNS ANYWAY and lets the open proceed. That open may then
// block forever, which is the pre-existing behaviour — this function's job is
// to make the common case work and to leave a log line naming the holder when
// it does not. Refusing to open would be a bigger behaviour change than the
// bug warrants, and the proper fix for a permanently-held device is to stop
// gating the rest of main() on the speaker at all.
func waitForFreePcm(card, device int, timeout time.Duration) {
	path := statusPath(card, device)
	b, err := os.ReadFile(path)
	if err != nil || pcmFree(string(b)) {
		return // free, or no status file to consult — open immediately
	}

	log.Printf("[speaker] %s held by pid %d — waiting up to %s", path, pcmOwner(string(b)), timeout)
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		time.Sleep(200 * time.Millisecond)
		b, err := os.ReadFile(path)
		if err != nil || pcmFree(string(b)) {
			log.Printf("[speaker] speaker released after %s", time.Since(deadline.Add(-timeout)).Round(time.Millisecond))
			return
		}
	}
	b, _ = os.ReadFile(path)
	log.Printf("[speaker] speaker STILL held by pid %d after %s — opening anyway, this may block",
		pcmOwner(string(b)), timeout)
}

// EnableSpeakerAmp switches the internal speaker amplifier back on.
//
// accdet turns it off when a plug is inserted (correctly — the Dot should not
// play to the room while headphones are connected) and NOTHING turns it back
// on when the plug is removed. Init is otherwise the only thing that ever sets
// it, which is why the speaker stayed silent until the next reboot (#80).
//
// Note this is the ONLY control involved: output routing is done in hardware
// by the jack's switch contacts, so there is no mux or headphone-amp control
// to drive alongside it.
func (p *PcmSpeaker) EnableSpeakerAmp() {
	if out, err := exec.Command("tinymix", "-D", "0", "5", "On").CombinedOutput(); err != nil {
		log.Printf("[speaker] could not re-enable speaker amp: %v — %s", err, strings.TrimSpace(string(out)))
		return
	}
	log.Println("[speaker] speaker amp re-enabled")
}

// silenceLoop is the ALSA write path: every period, take what each plane has
// to offer, mix them, and pump. When neither has audio it pumps silence,
// which is what paces this loop — ALSA blocks in Pump at realtime rate, so
// there is no timer anywhere and no busy-waiting.
//
// It replaced a blocking select on a single channel. Two planes cannot be
// waited on that way without one starving the other, and mixing needs both
// in hand at the same instant; a non-blocking take from each, with silence
// as the floor, keeps the pacing property that made the original correct.
//
// Closes deadCh on any exit so blocked pump callers unblock with an error
// rather than hanging.
func (p *PcmSpeaker) silenceLoop() {
	defer close(p.deadCh)
	for {
		select {
		case <-p.stopCh:
			return
		default:
		}

		var voice, music []byte
		if p.voice.ready(primePeriods) {
			voice = p.voice.take()
		} else if p.voice.playing {
			p.report(p.voice.drained(), "voice")
		}
		if p.music.ready(primePeriods) {
			music = p.music.take()
		} else if p.music.playing {
			p.report(p.music.drained(), "music")
		}

		// The ring's level must be measured BEFORE mixing: Mix sums into the
		// voice buffer in place, so afterwards there is no voice-only signal
		// left to measure.
		var level float64
		if voice != nil && p.levelTap != nil {
			level = periodRMS(voice)
		}

		out := p.mixer.Mix(voice, music, p.duckTarget.Load())
		if out == nil {
			out = silencePeriod
		}

		// A device-generated cue sums in BEFORE the chain, so it is shaped
		// and limited with everything else and can never push the sum into
		// clipping when it lands on top of loud music.
		out = p.mixCue(out)

		// ─── Output chain: EQ, bass guard, limiter ───────────────────────
		//
		// POST-MIX, and that is the whole point (docs/audio-states.md §8).
		// Controller-side there are two independent chains — one for voice,
		// one for music — so neither limiter ever sees the two summed, and
		// only the mixer's saturation stands behind a loud response over
		// loud music. Here there is one chain and it sees what the speaker
		// actually emits.
		//
		// It runs BEFORE the taps for the same reason the taps were moved
		// after the mix: the AEC far-end reference has to be what the
		// speaker emits, and after this stage that is no longer the mix.
		out = p.applyOutputChain(out)

		// Taps see the MIXED output, which is what the speaker actually
		// emits. That matters for AEC: the far-end reference is what needs
		// cancelling from the mic, and with music playing under a response
		// the echo is the sum, not the voice alone. It was already correct
		// by accident when only one stream existed.
		if p.echoTap != nil {
			p.echoTap(out)
		}
		// VOICE only, deliberately — unlike the echo tap above. The meter
		// ring visualises the RESPONSE; feeding it the mix made the ring
		// throb along to ducked music before the response had started, which
		// reads as the device doing something it is not.
		if p.levelTap != nil {
			p.levelTap(level)
		}
		if err := p.session.Pump(out); err != nil {
			log.Printf("silenceLoop: pump error: %v", err)
			return
		}
	}
}

// report logs and forwards a completed stream's stats. A nil st is an
// underrun, which is counted inside the stream and only logged here.
func (p *PcmSpeaker) report(st *StreamStats, plane string) {
	if st == nil {
		log.Printf("[speaker] UNDERRUN: %s channel drained mid-stream — injecting silence", plane)
		return
	}
	log.Printf("[speaker] %s stream complete — returning to silence "+
		"(periods=%d underruns=%d minDepth=%d primeWait=%dms recvSpan=%dms maxGap=%dms)",
		plane, st.Periods, st.Underruns, st.MinDepth,
		st.PrimeWaitMs, st.RecvSpanMs, st.MaxGapMs)
	// Only the voice plane reports upstream: the controller attaches these
	// to the turn that produced them (device.last_turn_id), and a music
	// stream ending would overwrite a turn's delivery figures with a song's.
	if plane != "voice" || st.Periods == 0 {
		return
	}
	p.statsMu.Lock()
	cb := p.statsCb
	p.statsMu.Unlock()
	if cb != nil {
		go cb(*st)
	}
}

// toStereo converts a mono S16 wire period into the stereo frames the ALSA
// device requires. The stereo config is an I2S/codec-path constraint, not a
// wire one — shipping two identical channels would double bandwidth for
// nothing on links that are already marginal.
func toStereo(data []byte) []byte {
	n := len(data) / 2
	period := make([]byte, n*4)
	for i := 0; i < n; i++ {
		lo, hi := data[i*2], data[i*2+1]
		period[i*4+0], period[i*4+1] = lo, hi // L
		period[i*4+2], period[i*4+3] = lo, hi // R
	}
	return period
}

// PumpPeriod queues one period of VOICE audio (TTS, announcements). Called by
// the WS client for each incoming 0x02 frame. Blocks until the ALSA loop has
// consumed a slot (rate-limiting to playback speed), or returns an error if
// that loop has died — preventing an infinite block on a dead consumer.
func (p *PcmSpeaker) PumpPeriod(data []byte) error {
	_, err := p.voice.pump(toStereo(data), len(data))
	return err
}

// PumpMusic queues one period of MUSIC audio (0x04). Identical handling to
// the voice plane — it is a full stream with its own prime gate, buffer and
// discard semantics, not a lesser one — but kept separate so a voice turn
// can duck it rather than stopping it.
func (p *PcmSpeaker) PumpMusic(data []byte) error {
	_, err := p.music.pump(toStereo(data), len(data))
	return err
}

// SetDuck sets the gain applied to music while it plays under voice, in dB of
// attenuation (0 = no ducking). The change is ramped by the mixer rather than
// applied at once, because a gain step at a period boundary is a click — and
// it would land on exactly the moment the user started speaking.
func (p *PcmSpeaker) SetDuck(db float64) {
	p.duckTarget.Store(DuckGain(db))
}

// IsStreaming reports whether a VOICE stream is currently mid-flight.
//
// Added for on-device wake word scoring: while the speaker is playing, the
// controller lowers its wake threshold to bargeInThreshold, because echo at the
// mic is ~25dB louder than the person and speech-over-TTS scores are depressed.
// The device has to know when it is playing in order to mirror that, or it
// disagrees with the controller on every barge-in.
//
// Music deliberately does NOT count here. It is a quieter, continuous bed
// rather than a response the user is talking over, and the controller applies
// the same rule on its side (wake-over-music scores against bargeInThreshold
// only when barge-in is enabled). Reporting music as "streaming" would drop
// the device's bar for as long as a song plays.
func (p *PcmSpeaker) IsStreaming() bool { return p.voice.isActive() }

// IsPlayingMusic reports whether a music stream is mid-flight.
func (p *PcmSpeaker) IsPlayingMusic() bool { return p.music.isActive() }

// EndStream marks the in-flight voice stream complete (0x03). Always arrives
// after every 0x02 period of that stream has been handed to PumpPeriod —
// frames are processed sequentially on the read loop — so by the time the
// ALSA loop drains the buffer the flag is already set.
func (p *PcmSpeaker) EndStream() { p.voice.endStream() }

// EndMusicStream marks the in-flight music stream complete (0x05).
func (p *PcmSpeaker) EndMusicStream() { p.music.endStream() }

// Flush cuts a playing VOICE stream immediately (barge-in). Two parts:
//  1. Drain the buffer — kills up to ~5.5s already queued on-device.
//  2. Arm discarding (if a stream is mid-flight) — subsequent periods of
//     this stream are dropped until its EOS arrives. Necessary because the
//     controller writes the whole response into the WebSocket ahead of
//     playback: at barge time the rest of the stream is already in TCP
//     buffers and would refill the channel right after the drain (the
//     pre-2026-07-08 version drained only, and playback resumed after a
//     ~1.3s skip). The controller sends the EOS on the cancel path too, so
//     the discard always terminates.
//
// Up to PeriodCount ALSA periods (~170ms) already handed to the hardware
// still play — cutting those needs a stream restart, which costs more in
// click/pop than it saves.
func (p *PcmSpeaker) Flush() { p.voice.flush() }

// FlushMusic stops music the same way. This is now reserved for the user
// GENUINELY stopping or pausing playback: a voice turn ducks instead, which
// is the whole point of holding the two planes apart. Flushing here would
// throw away the buffered audio that makes ducking instant, and on a
// non-seekable stream that audio cannot be recovered.
func (p *PcmSpeaker) FlushMusic() { p.music.flush() }

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

// periodRMS computes the RMS level of a stereo S16LE period, normalized to
// 0..1 of int16 full-scale. Left channel only, every 4th frame — the wire
// is mono duplicated L=R and the LED meter needs ~2 significant digits at
// ~23Hz, so 512 of 2048 frames is plenty at a quarter of the cost. Runs on
// the ALSA pump goroutine: no allocation, integer accumulate.
func periodRMS(period []byte) float64 {
	if len(period) < 4 {
		return 0
	}
	var sum uint64
	n := 0
	// Stereo frame = 4 bytes (L16+R16); step 4 frames = 16 bytes.
	for i := 0; i+1 < len(period); i += 16 {
		s := int64(int16(uint16(period[i]) | uint16(period[i+1])<<8))
		sum += uint64(s * s)
		n++
	}
	if n == 0 {
		return 0
	}
	return math.Sqrt(float64(sum)/float64(n)) / 32768.0
}
