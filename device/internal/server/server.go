package server

import (
	"log"
	"math"
	"sync"
	"sync/atomic"
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

	// anim owns the device-rendered ring animation (led_anim messages).
	anim animator

	// audioLevel holds the live speaker RMS as float64 bits — written by
	// the speaker's ALSA pump via SetAudioLevel, read by the meter anim.
	audioLevel atomic.Uint64
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

	// When the volume arc's display window ends, hand the ring back to the
	// last controller-set state (listening/thinking/playing mid-turn, all
	// off when idle). SetLEDs keeps recording frames into baseLEDs during
	// the window — it just doesn't paint them — so this repaint lands on
	// the current animation frame, not a stale one.
	server.volume.onDisplayExpire = func() {
		server.paintBaseLEDs()
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

		// Discrete red LED under the mic-off button (GPIO, separate from
		// the ring) — export + off. Non-fatal: an unmuted boot without a
		// button LED is cosmetic, everything else still works.
		if err := internalLed.InitMuteButtonLED(); err != nil {
			log.Printf("Mute button LED init failed: %v", err)
		}
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
	s.volume.SetOnVolumeChange(cb)
}

// MuteToggle toggles mic mute state — called by button handler.
func (s *Server) MuteToggle() {
	s.mute.Toggle()
}

// SetMuteChangeCallback wires a callback invoked when mute state changes.
func (s *Server) SetMuteChangeCallback(cb func(muted bool)) {
	s.mute.SetOnMuteChange(cb)
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
	// Same paint suppressions as SetLEDs: the volume arc owns the ring for
	// its display window, and the mute ring is device-sovereign.
	if s.volume.DisplayActive() || s.mute.IsMuted() {
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
	primary := ((normAngle - ledOffset + 360) % 360) / 30 % nLEDs
	secondary := (primary + 1) % nLEDs
	tertiary := (primary + nLEDs - 1) % nLEDs

	s.baseLEDsMu.Lock()
	base := s.baseLEDs
	s.baseLEDsMu.Unlock()

	leds := make([]led.Led, nLEDs)
	for i := range leds {
		leds[i] = base[i]
		leds[i].ID = i
	}

	// Scene-agnostic highlight: brighten the base ring colour toward white
	// rather than painting hardcoded green — the listening ring can be any
	// colour now (LED scenes), and a green marker on e.g. a crimson ring
	// read as a glitch. Primary gets a strong lift, neighbours a soft one.
	brighten := func(l led.Led, add int) led.Led {
		l.R = clampAdd(l.R, add)
		l.G = clampAdd(l.G, add)
		l.B = clampAdd(l.B, add)
		return l
	}
	leds[primary] = brighten(base[primary], 150)
	leds[secondary] = brighten(base[secondary], 60)
	leds[tertiary] = brighten(base[tertiary], 60)
	leds[primary].ID, leds[secondary].ID, leds[tertiary].ID = primary, secondary, tertiary

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
//
// Two conditions suppress the hardware paint (state is still recorded in
// baseLEDs so the ring can be restored later):
//   - volume display window: the turn animations repaint continuously, so
//     without this the volume arc survives ~one frame and reads as a
//     glitch. The window's expiry repaints baseLEDs (see onDisplayExpire).
//   - muted: the red ring is device-sovereign. Turns could not previously
//     overlap mute (mic stopped), but mute-terminates-turn (2026-07-10)
//     means the cancelled turn's LED cleanup arrives after the red ring
//     is up — it must not clear it. Unmute clears the ring explicitly.
// listeningHint is the controller's explicit "this frame is the listening
// ring" flag (nil from pre-scene controllers). When absent, fall back to
// the historical heuristic — a 12-LED all-green frame — which only works
// for the standard scene.
func (s *Server) SetLEDs(leds []led.Led, listeningHint *bool) {
	s.LEDModeSystem()
	var listeningRing bool
	if listeningHint != nil {
		listeningRing = *listeningHint
	} else {
		listeningRing = len(leds) == 12
		if listeningRing {
			for _, l := range leds {
				if l.R != 0 || l.B != 0 || l.G == 0 {
					listeningRing = false
					break
				}
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
	if s.volume.DisplayActive() || s.mute.IsMuted() {
		return
	}
	s.paintBaseLEDs()
}

// paintBaseLEDs paints the ring from the stored controller state.
func (s *Server) paintBaseLEDs() {
	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()
	if lc == nil {
		return
	}
	s.baseLEDsMu.Lock()
	base := s.baseLEDs
	s.baseLEDsMu.Unlock()
	leds := make([]led.Led, len(base))
	for i := range base {
		leds[i] = base[i]
		leds[i].ID = i
	}
	if err := lc.SetLEDs(leds...); err != nil {
		log.Printf("SetLEDs error: %v", err)
	}
}
