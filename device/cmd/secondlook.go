package main

import (
	"log"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/server"
	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/turnbeam"
	"github.com/wilbowes/EchoMuse/internal/wakeword"
	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
)

// The second look (turnbeam.Probe): a near-miss on the wake scorer (a run of
// frames reaching nearMissBar that ends without a crossing) has the turn
// stage beam its own wake window, and a scorer kept for this alone scores
// that one beam. At the wake bar it is a wake, through onWakeCrossing like
// any other: the same mute, wake-word-off and private-listening rules, dated
// from the near-miss's peak frame. Only while the turn stage is on, only
// under private listening, and not while a reply plays (the barge path has
// its own scorers).
const (
	nearMissBar = 0.1
	// lookFrom/lookTo: the beam's scores count over this span around the
	// near-miss's peak frame.
	lookFrom, lookTo = -0.6, 0.6
)

// SecondLookBenchOn, set only by bench builds, switches the second look on.
// Off otherwise: offline its gain was 1 of 37 attempts in this code (0-3
// across equally valid details of the steering), too fragile to ship.
var SecondLookBenchOn func() bool

var lookState struct {
	mu sync.Mutex
	sc *shadow.Scorer
}

// syncSecondLook gives the wake scorer sc a second look, with its own
// scorer for the model, or takes it away (sc nil). Called whenever the wake
// scorer is rebuilt.
func syncSecondLook(dc *client.DataClient, cc *client.ControlClient, spk *speaker.PcmSpeaker,
	srv *server.Server, sc *shadow.Scorer, model string) {
	lookState.mu.Lock()
	old := lookState.sc
	lookState.sc = nil
	lookState.mu.Unlock()
	if old != nil {
		old.Close()
	}
	if sc == nil || !dc.TurnStageOn() || SecondLookBenchOn == nil || !SecondLookBenchOn() {
		return
	}
	look, err := shadow.Open(model, 1, nil)
	if err != nil {
		log.Printf("[look] second look off: %v", err)
		return
	}
	lookState.mu.Lock()
	lookState.sc = look
	lookState.mu.Unlock()
	sc.SetNearMiss(nearMissBar, func(peak float32, at time.Time) {
		if dc.ListenState() != client.ListenLocal || (spk != nil && spk.VoiceAudible(wakeword.ScoreSpan)) {
			return
		}
		dc.SecondLook(at, func(p *turnbeam.Probe, err error) {
			secondLook(dc, cc, spk, srv, look, peak, at, p, err)
		})
	})
	log.Printf("[look] second look on near-misses from %.2f", nearMissBar)
}

func secondLook(dc *client.DataClient, cc *client.ControlClient, spk *speaker.PcmSpeaker,
	srv *server.Server, look *shadow.Scorer, peak float32, at time.Time, p *turnbeam.Probe, err error) {
	if err != nil {
		log.Printf("[look] near-miss %.2f: no probe (%v)", peak, err)
		return
	}
	t0 := time.Now()
	scores := look.ScoreClip(p.Beam())
	var best float32
	for i, v := range scores {
		if t := p.Start() + float64(i+1)*0.08; t >= lookFrom && t <= lookTo && v > best {
			best = v
		}
	}
	threshold := float32(config.Get().Snapshot().OwwThreshold)
	log.Printf("[look] near-miss %.2f: beam scored %.2f (bar %.2f) in %dms",
		peak, best, threshold, time.Since(t0).Milliseconds())
	if best >= threshold {
		onWakeCrossing(cc, dc, spk, srv, best, threshold, at)
	}
}
