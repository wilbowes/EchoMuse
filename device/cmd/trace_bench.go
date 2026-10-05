//go:build server && bench

package main

import (
	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/wakeword"
	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
)

// Bench only: log the per-frame wake scores around every near-miss and
// crossing (shadow.Scorer.SetTraceLabel), labelled with what the speaker was
// playing, to choose the wake bar over music from trajectories rather than
// peaks.
func init() {
	benchScorer = func(sc *shadow.Scorer, spk *speaker.PcmSpeaker) {
		sc.SetTraceLabel(playingLabel(spk))
	}
}

func playingLabel(spk *speaker.PcmSpeaker) func() string {
	return func() string {
		switch {
		case spk.VoiceAudible(wakeword.ScoreSpan):
			return "voice"
		case spk.MusicAudible(wakeword.ScoreSpan):
			return "music"
		}
		return "quiet"
	}
}
