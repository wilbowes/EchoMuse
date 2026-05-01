//go:build server

package speaker

import (
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

var silencePeriod = make([]byte, periodBytes)

type PcmSpeaker struct {
	session  *tinyalsa.AudioSession
	pauseCh  chan struct{}
	resumeCh chan struct{}
	stopCh   chan struct{}
}

func NewPcmSpeaker() (*PcmSpeaker, error) {
	s := &PcmSpeaker{
		pauseCh:  make(chan struct{}),
		resumeCh: make(chan struct{}),
		stopCh:   make(chan struct{}),
	}
	if err := s.Init(); err != nil {
		return nil, err
	}
	return s, nil
}

func (p *PcmSpeaker) Init() error {
	exec.Command("stop", "mixer").Run()
	exec.Command("tinymix", "-D", "0", "61", "0", "0").Run()  // mute before amp enable
	exec.Command("tinymix", "-D", "0", "5", "On").Run()        // enable amp
	time.Sleep(50 * time.Millisecond)                           // let amp settle
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

func (p *PcmSpeaker) silenceLoop() {
	for {
		// Check for pause signal before writing next period
		select {
		case <-p.stopCh:
			return
		case <-p.pauseCh:
			<-p.resumeCh
			continue
		default:
		}

		if err := p.session.Pump(silencePeriod); err != nil {
			log.Printf("silenceLoop: pump error: %v", err)
			return
		}
	}
}

func (p *PcmSpeaker) Pump(data []byte) error {
	log.Printf("Pump called with %d bytes", len(data))

	// Pause silence goroutine — it will finish current period then block
	p.pauseCh <- struct{}{}

	for len(data) >= periodBytes {
		if err := p.session.Pump(data[:periodBytes]); err != nil {
			log.Printf("Pump error: %v", err)
			p.resumeCh <- struct{}{}
			return err
		}
		data = data[periodBytes:]
	}

	p.resumeCh <- struct{}{}
	return nil
}

func (p *PcmSpeaker) Close() {
	close(p.stopCh)
	p.session.Close()
}