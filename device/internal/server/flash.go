package server

import (
	"log"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// flashState is the ring's one-shot acknowledgement. While a flash is up,
// SetLEDs and SetDirectionLEDs record their frames but do not paint, so the
// link pulse (every 50ms) and the animator cannot cut it short.
type flashState struct {
	gen    uint64
	active bool
}

func (s *Server) flashActive() bool {
	s.flashMu.Lock()
	defer s.flashMu.Unlock()
	return s.flash.active
}

// Flash paints the whole ring one colour for d, then hands it back to
// whatever owns it: the mute ring, the volume arc, or the latest frame.
// It acknowledges a gesture that otherwise shows nothing, such as a pairing
// hold on a connected device.
func (s *Server) Flash(r, g, b uint8, d time.Duration) {
	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()
	if lc == nil {
		return
	}

	s.flashMu.Lock()
	s.flash.gen++
	s.flash.active = true
	gen := s.flash.gen
	s.flashMu.Unlock()

	leds := make([]led.Led, 12)
	for i := range leds {
		leds[i] = led.Led{ID: i, R: r, G: g, B: b}
	}
	if err := lc.SetLEDs(leds...); err != nil {
		log.Printf("Flash LED set failed: %v", err)
	}

	time.AfterFunc(d, func() {
		s.flashMu.Lock()
		if s.flash.gen != gen {
			s.flashMu.Unlock()
			return // a newer flash owns the ring
		}
		s.flash.active = false
		s.flashMu.Unlock()
		s.restoreRing()
	})
}

// restoreRing repaints the ring from state after something held it.
func (s *Server) restoreRing() {
	if s.volume.DisplayActive() {
		return // the arc repaints the base itself when it expires
	}
	if s.mute.IsMuted() && !s.LinkDown() {
		s.mute.showMuteLEDs()
		return
	}
	s.paintBaseLEDs()
}
