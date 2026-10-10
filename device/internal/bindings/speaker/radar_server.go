//go:build server

package speaker

import (
	"os"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bindings/mixer"
)

// Radar adds a physical mute and a longer settling sequence. Keep those
// requirements separate from the shared PCM loop and other boards' timing.
func (p *PcmSpeaker) startRadarOutput() error {
	readStatus := func() ([]byte, error) { return os.ReadFile(p.statusFile) }
	if err := waitForRunningPCM(readStatus, p.deadCh, 3*time.Second); err != nil {
		return err
	}
	return unmuteRadarSpeaker(func(d time.Duration) error { return waitForSilence(p.deadCh, d) })
}

// Do not close the native PCM while its writer is still using it.
func (p *PcmSpeaker) abortRadarStartup() {
	mixer.Set(radarMute, "On")
	mixer.Set(mixer.PlaybackVolume, "0")
	mixer.Set(mixer.SpeakerAmp, "Off")
	if p.session != nil {
		close(p.stopCh)
		<-p.deadCh
		p.session.Close()
	}
}
