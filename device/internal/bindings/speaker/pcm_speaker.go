//go:build server

package speaker

import (
	"fmt"
	"log"
	"os/exec"
	"time"

	"github.com/Binozo/GoTinyAlsa/pkg/pcm"
	"github.com/Binozo/GoTinyAlsa/pkg/tinyalsa"
)

const cardNr      = 0
const deviceNr    = 23
const periodSize  = 2048
const periodBytes = periodSize * 2 * 2 // 2 channels * 2 bytes = 8192

// audioCh depth — shallow so the WS sender gets backpressure if ALSA falls behind.
const audioChanDepth = 4

var silencePeriod = make([]byte, periodBytes)

type PcmSpeaker struct {
	session  *tinyalsa.AudioSession
	audioCh  chan []byte
	stopCh   chan struct{}
	// deadCh is closed by silenceLoop on any exit so PumpPeriod can return
	// an error rather than block indefinitely waiting for a dead consumer.
	deadCh   chan struct{}
}

func NewPcmSpeaker() (*PcmSpeaker, error) {
	s := &PcmSpeaker{
		audioCh: make(chan []byte, audioChanDepth),
		stopCh:  make(chan struct{}),
		deadCh:  make(chan struct{}),
	}
	if err := s.Init(); err != nil {
		return nil, err
	}
	return s, nil
}

func (p *PcmSpeaker) Init() error {
	exec.Command("stop", "mixer").Run()
	exec.Command("tinymix", "-D", "0", "61", "0", "0").Run()   // mute before amp enable
	exec.Command("tinymix", "-D", "0", "5", "On").Run()         // enable amp
	time.Sleep(50 * time.Millisecond)                            // let amp settle
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
func (p *PcmSpeaker) silenceLoop() {
	defer close(p.deadCh)
	for {
		select {
		case <-p.stopCh:
			return
		case period := <-p.audioCh:
			if err := p.session.Pump(period); err != nil {
				log.Printf("silenceLoop: pump error: %v", err)
				return
			}
		default:
			if err := p.session.Pump(silencePeriod); err != nil {
				log.Printf("silenceLoop: silence pump error: %v", err)
				return
			}
		}
	}
}

// Pump plays a complete buffer, period by period. Used by the HTTP speaker path
// (Phase 2: will be removed once speaker moves fully to WS streaming).
func (p *PcmSpeaker) Pump(data []byte) error {
	log.Printf("Pump called with %d bytes", len(data))
	for len(data) >= periodBytes {
		select {
		case p.audioCh <- data[:periodBytes]:
		case <-p.deadCh:
			return fmt.Errorf("speaker: ALSA loop has died")
		}
		data = data[periodBytes:]
	}
	return nil
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

func (p *PcmSpeaker) Close() {
	close(p.stopCh)
	p.session.Close()
}
