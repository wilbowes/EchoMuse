package server

import (
	"log"
	"math"
	"sync"
	"time"

	internalLed "github.com/wilbowes/EchoMuse/internal/bindings/led"
	"github.com/wilbowes/EchoMuse/pkg/buttons"
	"github.com/wilbowes/EchoMuse/pkg/led"
	"github.com/wilbowes/EchoMuse/pkg/mic"
	"github.com/wilbowes/EchoMuse/pkg/speaker"
	"golang.org/x/sys/unix"
)

// ledMode controls which subsystem currently owns the LED ring.
// Higher value = higher priority.
type ledMode int

const (
	ledModeDirection ledMode = iota // beamformer arc — lowest priority
	ledModeSystem                   // controller/mute/pulse — highest priority
)

type Server struct {
	ledController    led.Controller
	ledMu            sync.Mutex
	buttonController buttons.Controller
	mic              mic.Microphone
	speaker          speaker.Speaker
	volume           *volumeController
	mute             *muteController

	ledModeMu sync.Mutex
	ledMode   ledMode

	// baseLEDs stores the controller-set ring state so direction overlay
	// can always be applied fresh on top without accumulating.
	baseLEDs   [12]led.Led
	baseLEDsMu sync.Mutex

	// listeningLEDs is true when the controller has set the solid green
	// listening ring — the only state where direction overlay is shown.
	listeningLEDs bool
}

func NewServer(buttonController buttons.Controller, microphone mic.Microphone, speaker speaker.Speaker) *Server {
	server := &Server{
		buttonController: buttonController,
		mic:              microphone,
		speaker:          speaker,
	}

	// Volume controller uses a getter so it handles the nil-during-boot window safely
	server.volume = newVolumeController(func() led.Controller {
		server.ledMu.Lock()
		defer server.ledMu.Unlock()
		return server.ledController
	})

	// Mute controller — same LED getter pattern
	server.mute = newMuteController(func() led.Controller {
		server.ledMu.Lock()
		defer server.ledMu.Unlock()
		return server.ledController
	}, nil)

	// Give volume controller access to mute state so it can restore the red ring
	server.volume.isMuted = func() bool {
		return server.mute.IsMuted()
	}

	go func() {
		uptime, err := getUptime()
		// Reduced from 90 seconds as server is started at the end of the boot cycle anyway.
		minUptime := time.Second * 5

		if err != nil || uptime < minUptime {
			// If we start too soon the native bootup from the echo will break (LEDs will spin forever)
			stillWait := minUptime - uptime
			log.Printf("Uptime is currently at %0.2fs, waiting %0.2fs for LED setup\n", uptime.Seconds(), stillWait.Seconds())
			time.Sleep(stillWait)
		}

		ledController, err := internalLed.NewDefaultController()
		if err != nil {
			log.Fatalf("Failed to initialize LED controller: %v", err)
		}

		server.ledMu.Lock()
		server.ledController = ledController
		server.ledMu.Unlock()
		clearLeds(ledController)
	}()

	return server
}

// VolumeStepUp increases volume one step — called by button handler.
func (s *Server) VolumeStepUp() {
	s.volume.StepUp()
}

// VolumeStepDown decreases volume one step — called by button handler.
func (s *Server) VolumeStepDown() {
	s.volume.StepDown()
}

// SetVolume sets volume to an explicit level (0–175) — called by controller command.
func (s *Server) SetVolume(level int) {
	s.volume.Set(level)
}

// VolumeLevel returns the current volume level (0–175).
func (s *Server) VolumeLevel() int {
	return s.volume.Get()
}

// SetVolumeChangeCallback wires a callback invoked when volume changes.
// The callback receives the new level (0–175).
func (s *Server) SetVolumeChangeCallback(cb func(level int)) {
	s.volume.mu.Lock()
	s.volume.onVolumeChange = cb
	s.volume.mu.Unlock()
}

// MuteToggle toggles mic mute state — called by button handler.
func (s *Server) MuteToggle() {
	s.mute.Toggle()
}

// SetMuteChangeCallback wires a callback invoked when mute state changes.
func (s *Server) SetMuteChangeCallback(cb func(muted bool)) {
	s.mute.mu.Lock()
	s.mute.onMuteChange = cb
	s.mute.mu.Unlock()
}

// IsMuted returns true when the mic is muted — used to block dot button.
func (s *Server) IsMuted() bool {
	return s.mute.IsMuted()
}

// RestoreMuteRing re-applies the red mute ring. Called on reconnect to
// recover the visual state that the orange pulse animation overwrote.
func (s *Server) RestoreMuteRing() {
	s.mute.showMuteLEDs()
}

func clearLeds(ledController led.Controller) {
	numLEDs, err := ledController.GetNumLEDs()
	if err != nil {
		log.Printf("clearLeds: failed to get LED count: %v", err)
		return
	}

	leds := make([]led.Led, numLEDs)
	for i := 0; i < numLEDs; i++ {
		leds[i] = led.Led{
			ID: i,
			R:  0,
			G:  0,
			B:  0,
		}
	}
	if err = ledController.SetLEDs(leds...); err != nil {
		log.Printf("clearLeds: failed to set LEDs: %v", err)
	}
}

func getUptime() (time.Duration, error) {
	var info unix.Sysinfo_t
	if err := unix.Sysinfo(&info); err != nil {
		return time.Duration(0), err
	}
	return time.Second * time.Duration(info.Uptime), nil
}

// SetLEDMode sets the current LED priority mode.
func (s *Server) SetLEDMode(m ledMode) {
	s.ledModeMu.Lock()
	s.ledMode = m
	s.ledModeMu.Unlock()
}

// LEDModeSystem claims the LED ring for system use.
func (s *Server) LEDModeSystem() { s.SetLEDMode(ledModeSystem) }

// LEDModeDirection releases the LED ring back to the beamformer arc.
func (s *Server) LEDModeDirection() { s.SetLEDMode(ledModeDirection) }

// SetDirectionLEDs overlays a direction marker onto the current LED ring state.
func (s *Server) SetDirectionLEDs(angleDeg float64) {
	if angleDeg < 0 {
		return
	}

	s.baseLEDsMu.Lock()
	listening := s.listeningLEDs
	s.baseLEDsMu.Unlock()
	if !listening {
		return
	}

	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()
	if lc == nil {
		return
	}

	const (
		nLEDs     = 12
		ledOffset = 240
	)

	normAngle := int(math.Round(angleDeg/30)) * 30
	primary   := ((normAngle - ledOffset + 360) % 360) / 30 % nLEDs
	secondary := (primary + 1) % nLEDs
	tertiary  := (primary + nLEDs - 1) % nLEDs

	s.baseLEDsMu.Lock()
	base := s.baseLEDs
	s.baseLEDsMu.Unlock()

	leds := make([]led.Led, nLEDs)
	for i := range leds {
		leds[i] = base[i]
		leds[i].ID = i
	}

	leds[primary]   = led.Led{ID: primary,   R: 0, G: 255, B: 80}
	leds[secondary] = led.Led{ID: secondary, R: 0, G: clampAdd(base[secondary].G, 60), B: 0}
	leds[tertiary]  = led.Led{ID: tertiary,  R: 0, G: clampAdd(base[tertiary].G,  60), B: 0}

	if err := lc.SetLEDs(leds...); err != nil {
		log.Printf("SetDirectionLEDs error: %v", err)
	}
}

// clampAdd adds delta to v, clamping to 255.
func clampAdd(v uint8, delta int) uint8 {
	result := int(v) + delta
	if result > 255 {
		return 255
	}
	return uint8(result)
}

// SetLEDs applies LED state directly — called by the controller client.
func (s *Server) SetLEDs(leds []led.Led) {
	s.LEDModeSystem()
	listeningRing := len(leds) == 12
	if listeningRing {
		for _, l := range leds {
			if l.R != 0 || l.B != 0 || l.G == 0 {
				listeningRing = false
				break
			}
		}
	}
	s.baseLEDsMu.Lock()
	for _, l := range leds {
		if l.ID >= 0 && l.ID < 12 {
			s.baseLEDs[l.ID] = l
		}
	}
	s.listeningLEDs = listeningRing
	s.baseLEDsMu.Unlock()
	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()
	if lc == nil {
		return
	}
	if err := lc.SetLEDs(leds...); err != nil {
		log.Printf("SetLEDs error: %v", err)
	}
}
