package server

import (
	"log"
	"math"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// identifyDuration is how long the Echo flashes when the dashboard asks.
// The device owns it: no controller failure can leave the ring held.
const identifyDuration = 30 * time.Second

const (
	identifyPeriod = 600 * time.Millisecond
	identifyTick   = 50 * time.Millisecond
)

// identifyPeak is the pulse's brightest colour. Red is mute, orange is "no
// controller", cyan is the volume arc, white is pending and amber is a ringing
// timer, so violet cannot be mistaken for any of them.
var identifyPeak = [3]float64{150, 70, 255}

// identifyState is the ring's top layer: "which Echo is this?". While it is
// active every write to the ring except its own is dropped (see gatedRing),
// whatever else owns the ring: a turn, a reply, the mute ring, the volume arc,
// the link pulse or a flash. Those keep updating their state underneath, and
// the ring is handed back to whichever of them applies when identify ends.
//
// The mute BUTTON's LED is a separate GPIO and is untouched, so it keeps
// showing mute for free.
type identifyState struct {
	gen      uint64
	active   bool
	start    time.Time
	deadline time.Time
}

func (s *Server) identifyActive() bool {
	s.identifyMu.Lock()
	defer s.identifyMu.Unlock()
	return s.identify.active
}

// gatedRing is the ring controller every painter except identify writes
// through. It is the one chokepoint: the volume arc, the mute ring, a flash
// and the animators all paint directly, so guarding SetLEDs alone would leave
// them free to stomp identify.
type gatedRing struct {
	s  *Server
	lc led.Controller
}

func (g gatedRing) Init() error              { return g.lc.Init() }
func (g gatedRing) GetNumLEDs() (int, error) { return g.lc.GetNumLEDs() }
func (g gatedRing) SetLEDs(l ...led.Led) error {
	if g.s.identifyActive() {
		return nil
	}
	return g.lc.SetLEDs(l...)
}

// gatedLED returns the ring behind the identify gate, or an untyped nil while
// the LED hardware is not up yet, so callers' nil checks keep working.
func (s *Server) gatedLED() led.Controller {
	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()
	if lc == nil {
		return nil
	}
	return gatedRing{s: s, lc: lc}
}

// identifyFrame is the whole ring at a point in the pulse (phase 0..1):
// brightness swings between 15% and 100% of the peak colour.
func identifyFrame(phase float64) []led.Led {
	b := 0.15 + 0.85*(0.5-0.5*math.Cos(2*math.Pi*phase))
	frame := make([]led.Led, numLEDs)
	for i := range frame {
		frame[i] = led.Led{
			ID: i,
			R:  uint8(math.Round(identifyPeak[0] * b)),
			G:  uint8(math.Round(identifyPeak[1] * b)),
			B:  uint8(math.Round(identifyPeak[2] * b)),
		}
	}
	return frame
}

// Identify is the dashboard's request: the ring for identifyDuration.
func (s *Server) Identify() { s.StartIdentify(identifyDuration) }

// StartIdentify takes the whole ring for d. Asked again while running it only
// moves the deadline, so the pulse keeps its phase (a restart reads as a
// flicker, the repeating-gif bug the link pulse once had).
func (s *Server) StartIdentify(d time.Duration) {
	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()
	if lc == nil {
		return
	}

	now := time.Now()
	s.identifyMu.Lock()
	if s.identify.active {
		s.identify.deadline = now.Add(d)
		s.identifyMu.Unlock()
		return
	}
	s.identify.gen++
	s.identify.active = true
	s.identify.start = now
	s.identify.deadline = now.Add(d)
	gen, start := s.identify.gen, now
	s.identifyMu.Unlock()

	// Painted before the goroutine's first tick, on the raw controller: the
	// gate is already closed to everyone else.
	if err := lc.SetLEDs(identifyFrame(0)...); err != nil {
		log.Printf("identify LED set failed: %v", err)
	}
	go s.runIdentify(gen, start, lc)
}

func (s *Server) runIdentify(gen uint64, start time.Time, lc led.Controller) {
	ticker := time.NewTicker(identifyTick)
	defer ticker.Stop()
	for range ticker.C {
		s.identifyMu.Lock()
		if s.identify.gen != gen || !s.identify.active {
			s.identifyMu.Unlock()
			return
		}
		if time.Now().After(s.identify.deadline) {
			s.identify.active = false
			s.identifyMu.Unlock()
			s.finishIdentify()
			return
		}
		s.identifyMu.Unlock()

		phase := math.Mod(float64(time.Since(start))/float64(identifyPeriod), 1)
		if err := lc.SetLEDs(identifyFrame(phase)...); err != nil {
			log.Printf("identify LED set failed: %v", err)
		}
	}
}

// finishIdentify hands the ring to whoever owns it underneath. A volume arc
// whose window is still open was dropped, so it is repainted; its own expiry
// then hands back as usual.
func (s *Server) finishIdentify() {
	if s.volume.DisplayActive() {
		s.volume.showLEDs(s.volume.Get())
		return
	}
	s.restoreRing()
}
