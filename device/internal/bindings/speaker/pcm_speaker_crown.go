//go:build crown

package speaker

import (
	"log"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/internal/alsa"
)

// crown: speaker is card0,device0 (MultiMedia1_Playback -> RT5616), measured
// working 2026-08-26. See docs/echo-show-8-hardware-map.md.
const cardNr = 0
const deviceNr = 0

const periodSize = 1536
const periodBytes = periodSize * 2 * 2 // 2ch * S16LE

const monoPeriodBytes = periodSize * 2

// Same reasoning as biscuit's constants (pcm_speaker.go) — unmeasured for
// crown's link characteristics, carried over as a starting point rather than
// re-derived, since nothing here is board-specific.
const audioChanDepth = 128
const primePeriods = 24

var silencePeriod = make([]byte, periodBytes)

// PcmSpeaker is crown's playback binding: same two-plane
// buffer/mixer/ducking machinery as biscuit (audioStream, Mixer — untagged,
// shared), a different ALSA client underneath. GoTinyAlsa needs libtinyalsa
// from the FireOS sysroot and has never been run against this driver;
// internal/alsa (used already for crown's HW_REFINE probing) needs neither.
// pcmWriter is satisfied by both *alsa.PCM (raw /dev/snd) and *socketPCM
// (feeds crown_launcher's AudioTrack service over a Unix socket instead —
// see socket_pcm_crown.go and docs/echo-show-8-journal.md, 2026-08-26
// entries). silenceLoop and Close only ever call Write/Close, so
// nothing else in this file needs to know which one it has.
type pcmWriter interface {
	Write(buf []byte) (int, error)
	Close() error
}

type PcmSpeaker struct {
	pcm    pcmWriter
	stopCh chan struct{}
	deadCh chan struct{}

	voice *audioStream
	music *audioStream

	// duckTarget is read every period on the ALSA goroutine and written from
	// SetDuck on the control-plane goroutine — atomic like biscuit's field
	// (pcm_speaker.go), same reason.
	duckTarget atomic.Int32
	mixer      Mixer

	echoTap  func([]byte)
	levelTap func(rms float64)

	statsCb func(StreamStats)
}

func (p *PcmSpeaker) OnStreamStats(cb func(StreamStats)) { p.statsCb = cb }

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

// Init brings the output stage up.
//
// Ext_Speaker_Amp_Switch is INVERTED on this board — On silences, Off is
// audible — confirmed by ear against a real unit 2026-08-26
// (docs/echo-show-8-hardware-map.md). Shipping the boot default (On) would
// silence the device with nothing logged, the exact trap checkers' README
// warns about for the same RT5616 codec.
func (p *PcmSpeaker) Init() error {
	// Playback no longer opens /dev/snd directly on crown — it feeds
	// crown_launcher's AudioTrack-backed service over a Unix socket
	// instead, so mediaserver arbitrates the DL1 hardware path normally
	// rather than contending with an exclusive hold on it (pstore evidence
	// of that contention wedging the DSP, the fix design, and the live
	// concurrent-load proof are all in docs/echo-show-8-journal.md,
	// 2026-08-26 entries, and docs/echo-show-8-audiotrack-design.md).
	// waitForFreePcm/alsa.Open are gone from this path entirely — the
	// socket has nothing to wait on, and connecting is handled lazily by
	// socketPCM itself (see socket_pcm_crown.go).
	p.pcm = newSocketPCM(crownPlaybackSocket)

	go p.silenceLoop()

	// Same pop-prevention wait as the raw-ALSA path had (silence reaching
	// the DAC before un-muting the external amp) — keeping it rather than
	// assuming AudioTrack's own startup has no equivalent transient, since
	// that hasn't been checked by ear yet on this path.
	time.Sleep(100 * time.Millisecond)
	if err := alsa.Apply(cardNr, []alsa.Control{
		{Name: "Ext_Speaker_Amp_Switch", Values: []string{"Off"}, Optional: true},
	}); err != nil {
		log.Printf("[speaker] amp enable: %v", err)
	}

	log.Println("PcmSpeaker (crown) initialised — playback via AudioTrack socket")
	return nil
}

func (p *PcmSpeaker) EnableSpeakerAmp() {
	if err := alsa.Apply(cardNr, []alsa.Control{
		{Name: "Ext_Speaker_Amp_Switch", Values: []string{"Off"}, Optional: true},
	}); err != nil {
		log.Printf("[speaker] could not re-enable speaker amp: %v", err)
		return
	}
	log.Println("[speaker] speaker amp re-enabled")
}

// silenceLoop mirrors biscuit's pump loop (pcm_speaker.go) — same
// mix/tap/report sequence — but p.pcm is a *socketPCM (see Init above), not
// a raw *alsa.PCM, so it does NOT get pacing for free the way a blocking
// ALSA Write does: a socket write returns as soon as the kernel buffer has
// room, which floods far faster than AudioTrack drains on the far end.
// socketPCM.Write does its own explicit real-time pacing internally
// (socket_pcm_crown.go) for exactly that reason — found the hard way
// (bursty delivery killed the connection after ~0.6-1.3s until pacing was
// added, docs/echo-show-8-audiotrack-design.md 2026-08-26). Nothing here
// needs to pace on top of that, but don't read this loop's timing as coming
// from an ALSA ring — it doesn't, on this path.
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

		var level float64
		if voice != nil && p.levelTap != nil {
			level = periodRMS(voice)
		}

		out := p.mixer.Mix(voice, music, p.duckTarget.Load())
		if out == nil {
			out = silencePeriod
		}

		if p.echoTap != nil {
			p.echoTap(out)
		}
		if p.levelTap != nil {
			p.levelTap(level)
		}
		if _, err := p.pcm.Write(out); err != nil {
			log.Printf("silenceLoop: write error: %v", err)
			return
		}
	}
}

func (p *PcmSpeaker) report(st *StreamStats, plane string) {
	if st == nil {
		log.Printf("[speaker] UNDERRUN: %s channel drained mid-stream — injecting silence", plane)
		return
	}
	log.Printf("[speaker] %s stream complete — returning to silence "+
		"(periods=%d underruns=%d minDepth=%d primeWaitMs=%dms recvSpan=%dms maxGap=%dms)",
		plane, st.Periods, st.Underruns, st.MinDepth,
		st.PrimeWaitMs, st.RecvSpanMs, st.MaxGapMs)
	if plane != "voice" || st.Periods == 0 {
		return
	}
	if p.statsCb != nil {
		go p.statsCb(*st)
	}
}

func (p *PcmSpeaker) PumpPeriod(data []byte) error {
	_, err := p.voice.pump(toStereo(data), len(data))
	return err
}

func (p *PcmSpeaker) PumpMusic(data []byte) error {
	_, err := p.music.pump(toStereo(data), len(data))
	return err
}

func (p *PcmSpeaker) SetDuck(db float64) { p.duckTarget.Store(DuckGain(db)) }

func (p *PcmSpeaker) IsStreaming() bool    { return p.voice.isActive() }
func (p *PcmSpeaker) IsPlayingMusic() bool { return p.music.isActive() }
func (p *PcmSpeaker) EndStream()           { p.voice.endStream() }
func (p *PcmSpeaker) EndMusicStream()      { p.music.endStream() }
func (p *PcmSpeaker) Flush()               { p.voice.flush() }
func (p *PcmSpeaker) FlushMusic()          { p.music.flush() }

// Close mutes before tearing the PCM down, same ordering as biscuit and for
// the same reason: an idle DAC with the amp still enabled hisses audibly for
// as long as the process is down.
func (p *PcmSpeaker) Close() {
	_ = alsa.Apply(cardNr, []alsa.Control{
		{Name: "Ext_Speaker_Amp_Switch", Values: []string{"On"}, Optional: true},
	})
	close(p.stopCh)
	p.pcm.Close()
	log.Println("PcmSpeaker (crown) closed — output muted, amp off")
}
