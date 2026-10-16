//go:build server

package speaker

import (
	"log"
	"os"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bindings/mixer"
	"github.com/wilbowes/EchoMuse/internal/outchain"
)

// radarTuning reads Radar's stock playback tuning from the Echo's own
// /system, for the output chain. Nil on any other board, and on a Radar
// whose files are missing or unreadable, which then plays without the
// stock curve.
func radarTuning(boardID string) *outchain.RadarTuning {
	if boardID != "radar" {
		return nil
	}
	rt, err := outchain.LoadRadarTuning(outchain.RadarTuningDir)
	if err != nil {
		log.Printf("[speaker] radar: no stock tuning from %s: %v", outchain.RadarTuningDir, err)
		return nil
	}
	log.Printf("[speaker] radar: stock tuning from %s: %s", outchain.RadarTuningDir, rt)
	return rt
}

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
