package server

import (
	"log"
	"math"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

const (
	volumeMin = 0

	// volumeMax is unity gain. The level keeps the DAC control's law (0.5dB
	// per step, 0dB at 127) though the volume is now applied in software
	// (speaker/swvolume.go), so levels mean what they always did. Above 127
	// the DAC applied positive gain to near-full-scale PCM and saturated —
	// measured 2026-08-13 at 65% THD by index 153, 89% by 170 — which is why
	// the scale stops here; device/CLAUDE.md, Volume, has the measurement.
	volumeMax = 127

	// volumeButtonFloor is the bottom of the band the PHYSICAL buttons
	// traverse, and what 1% means from Home Assistant or Sendspin
	// (PercentToLevel; controller em_volume.VOLUME_FLOOR is pinned to it). The
	// scale is dB-linear, so index 0 is -63.5dB and the bottom of the control
	// is indistinguishable from silence; stepping across it spends presses to
	// go nowhere. Silencing the device is the mute button's job, not the
	// volume button's.
	//
	// Explicit Set() calls are deliberately NOT floored — HA's volume 0.0
	// has to still mean silent. A press from below the floor lands ON the
	// floor rather than adding a step, so one press always reaches audible.
	volumeButtonFloor = 57 // -35dB

	// volumeStep is 1dB per press. The band holds arcStates levels, floor to
	// unity, one per state of the ring: 12 LEDs at three brightnesses each,
	// as stock does. So volumeSteps (35) presses cross it.
	volumeStep    = 2
	volumeSteps   = (volumeMax - volumeButtonFloor) / volumeStep
	arcStates     = volumeSteps + 1
	volumeLEDSecs = 2 // how long to show volume ring

	// volumeBoot is the level before the controller seeds the stored one:
	// what Init used to leave the DAC at, and so what this read back.
	volumeBoot = 100
	numLEDs    = 12
)

type volumeController struct {
	mu             sync.Mutex
	level          int
	ledCtrl        func() led.Controller // getter so we handle nil during boot
	timer          *time.Timer
	displayActive  bool        // volume arc currently on the ring — see DisplayActive
	isMuted        func() bool // set after construction to avoid circular dependency
	onVolumeChange func(int)   // set after construction; called after every Set()
	apply          func(int)   // applies a level to the audio; see SetApply
	// onDisplayExpire, when set, replaces the default clear-to-black at the
	// end of the display window: the server wires it to repaint the ring
	// from its stored controller state, so a volume press mid-turn hands
	// back to the listening/thinking/playing animation instead of going
	// dark. The muted → red-ring case stays here either way.
	onDisplayExpire func()
}

// DisplayActive reports whether the volume arc is currently on the ring.
// The server checks this to suppress controller LED paints (and the
// direction overlay) for the display window — without it, the turn
// animations repaint within one frame (~100ms) and the arc appears as a
// glitch rather than a reading.
func (vc *volumeController) DisplayActive() bool {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	return vc.displayActive
}

// SetOnVolumeChange wires a callback invoked after every Set() call.
// B7 fix (2026-07-05 review): previously Server.SetVolumeChangeCallback
// reached directly into vc.mu/vc.onVolumeChange from outside this struct.
// Encapsulating the lock here keeps volumeController responsible for its
// own synchronisation, matching every other volumeController method.
func (vc *volumeController) SetOnVolumeChange(cb func(int)) {
	vc.mu.Lock()
	vc.onVolumeChange = cb
	vc.mu.Unlock()
}

func newVolumeController(ledGetter func() led.Controller) *volumeController {
	vc := &volumeController{
		ledCtrl: ledGetter,
		level:   volumeBoot,
	}
	log.Printf("Volume controller initialised at %d/%d", vc.level, volumeMax)
	return vc
}

// SetApply wires what actually changes the loudness — the speaker's software
// volume — and applies the current level at once, since the speaker starts
// silent until told.
func (vc *volumeController) SetApply(fn func(int)) {
	vc.mu.Lock()
	vc.apply = fn
	level := vc.level
	vc.mu.Unlock()
	if fn != nil {
		fn(level)
	}
}

// Set applies a new volume level (0–volumeMax). showRing paints the cyan
// volume arc for the 2s display window — physical button presses pass true;
// live remote sets pass the remoteVolumeArc setting; the boot-time SeedVolume
// always passes false.
func (vc *volumeController) Set(level int, showRing bool) bool {
	level = clampVolumeLevel(level)

	vc.mu.Lock()
	changed := vc.level != level
	vc.level = level
	// Copy under the lock — SetOnVolumeChange writes this field under mu
	// from the main goroutine, and button events can fire before that
	// wiring completes (SubscribeToButton starts the evdev goroutines
	// first).
	cb := vc.onVolumeChange
	apply := vc.apply
	vc.mu.Unlock()

	if apply != nil {
		apply(level)
	}

	log.Printf("Volume set to %d/%d", level, volumeMax)
	if showRing {
		vc.showLEDs(level)
	}
	if cb != nil {
		cb(level)
	}
	return changed
}

func clampVolumeLevel(level int) int {
	if level < volumeMin {
		return volumeMin
	}
	if level > volumeMax {
		return volumeMax
	}
	return level
}

// CancelDisplay ends the volume arc's hold early, releasing the ring back to
// whatever wants to paint next.
//
// The hold exists to stop turn animations — which repaint every ~80ms — from
// stomping the arc within a frame of it appearing. It was never meant to
// outrank a deliberate press: adjusting the volume and immediately pressing
// the action button left the arc sitting there for the rest of its 2s with no
// sign the device had started listening.
//
// Deliberately does NOT repaint. The caller is about to start a turn, so its
// listening frame lands within a round trip; clearing to black here would put
// a visible dark gap between the two. The arc simply stops being sovereign.
func (vc *volumeController) CancelDisplay() {
	vc.mu.Lock()
	if vc.timer != nil {
		vc.timer.Stop()
		vc.timer = nil
	}
	vc.displayActive = false
	vc.mu.Unlock()
}

// Get returns current volume level.
func (vc *volumeController) Get() int {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	return vc.level
}

// StepUp increases volume by one step, within the button band.
func (vc *volumeController) StepUp() bool {
	vc.mu.Lock()
	level := stepLevel(vc.level, +1)
	vc.mu.Unlock()
	return vc.Set(level, true)
}

// StepDown decreases volume by one step, within the button band.
func (vc *volumeController) StepDown() bool {
	vc.mu.Lock()
	level := stepLevel(vc.level, -1)
	vc.mu.Unlock()
	return vc.Set(level, true)
}

// stepLevel is the level one press moves to: the next step of the button grid
// (volumeButtonFloor + n*volumeStep) in that direction. A level between steps
// — a stored level from before the grid — snaps to the neighbouring step, so
// the ring and the loudness agree again after one press.
func stepLevel(level, dir int) int {
	if level < volumeButtonFloor {
		return volumeButtonFloor
	}
	off := level - volumeButtonFloor
	n := off / volumeStep
	if dir > 0 {
		n++
	} else if off%volumeStep == 0 {
		n--
	}
	return clampToButtonBand(volumeButtonFloor + n*volumeStep)
}

// PercentToLevel maps a 0-100 volume (Home Assistant via the controller,
// Sendspin from Music Assistant) onto the button grid: 0 is silent, 1 the
// floor, 100 unity, so a percentage lands on exactly the level some number of
// presses would. Mirrors controller em_volume.ha_volume_to_device.
func PercentToLevel(pct float64) int {
	if pct <= 0 {
		return volumeMin
	}
	if pct >= 100 {
		return volumeMax
	}
	n := int(math.Round((pct - 1) / 99 * volumeSteps))
	return clampToButtonBand(volumeButtonFloor + max(n, 0)*volumeStep)
}

// LevelToPercent is PercentToLevel's inverse: 0 for silent, 1 for anything
// audible below the floor (0 would read as muted), else the level's place in
// the band. Mirrors controller em_volume.device_level_to_ha.
func LevelToPercent(level int) float64 {
	if level <= volumeMin {
		return 0
	}
	if level <= volumeButtonFloor {
		return 1
	}
	return 1 + float64(min(level, volumeMax)-volumeButtonFloor)/float64(volumeMax-volumeButtonFloor)*99
}

// PercentEcho converts a remote's 0-100 volume for one remote (Sendspin).
// The grid has fewer levels than there are percentages, so a commanded
// percentage does not always survive the trip to a level and back; but a
// remote's command must come back as the same number, or the two ends correct
// each other forever. So the last command is reported for as long as the
// level is still the one it set.
type PercentEcho struct {
	mu         sync.Mutex
	pct, level int
	set        bool
}

// ToLevel is PercentToLevel, remembering the command.
func (e *PercentEcho) ToLevel(pct int) int {
	level := PercentToLevel(float64(pct))
	e.mu.Lock()
	e.pct, e.level, e.set = pct, level, true
	e.mu.Unlock()
	return level
}

// ToPercent is LevelToPercent rounded, or the remembered command while the
// level is still the one it set.
func (e *PercentEcho) ToPercent(level int) int {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.set && e.level == level {
		return e.pct
	}
	return int(math.Round(LevelToPercent(level)))
}

// clampToButtonBand holds a stepped level inside [volumeButtonFloor,
// volumeMax].
func clampToButtonBand(level int) int {
	if level < volumeButtonFloor {
		return volumeButtonFloor
	}
	if level > volumeMax {
		return volumeMax
	}
	return level
}

// arcState is which of the volume arc's arcStates states a level shows (0
// for silent): its nearest button step, so a level set from Home Assistant
// reads the same as the presses that reach it. Anything audible shows at
// least the first, so the ring never reads as "off" when the device is
// merely quiet.
func arcState(level int) int {
	if level <= volumeMin {
		return 0
	}
	if level <= volumeButtonFloor {
		return 1
	}
	n := (level - volumeButtonFloor + volumeStep/2) / volumeStep
	return min(n+1, arcStates)
}

// arcLevels are an LED's three brightnesses, gamma-corrected so the thirds
// look like thirds (the drivers are linear PWM).
var arcLevels = [4]uint8{0, 26, 89, 200}

// arcLEDs lights the ring for an arc state: three states per LED, each LED
// filling to full before the next one starts. numLEDs*3 == arcStates.
func arcLEDs(state int) []led.Led {
	leds := make([]led.Led, numLEDs)
	for i := range leds {
		g := arcLevels[max(0, min(state-3*i, 3))]
		leds[i] = led.Led{ID: i, R: 0, G: g, B: g} // cyan
	}
	return leds
}

// showLEDs paints the volume arc for the level, then clears after 2s.
func (vc *volumeController) showLEDs(level int) {
	lc := vc.ledCtrl()
	if lc == nil {
		return
	}

	// The arc spans the BUTTON band, not the full control: over 0..volumeMax
	// the audible range crowds into the top LEDs.
	leds := arcLEDs(arcState(level))
	if err := lc.SetLEDs(leds...); err != nil {
		log.Printf("Volume LED set failed: %v", err)
		return
	}

	// Cancel any existing clear timer and start a new one
	vc.mu.Lock()
	vc.displayActive = true
	if vc.timer != nil {
		vc.timer.Stop()
	}
	vc.timer = time.AfterFunc(volumeLEDSecs*time.Second, func() {
		vc.mu.Lock()
		vc.displayActive = false
		expire := vc.onDisplayExpire
		vc.mu.Unlock()
		if vc.isMuted != nil && vc.isMuted() {
			// Restore mute indicator — red ring
			leds := make([]led.Led, numLEDs)
			for i := 0; i < numLEDs; i++ {
				leds[i] = led.Led{ID: i, R: 180, G: 0, B: 0}
			}
			lc.SetLEDs(leds...)
		} else if expire != nil {
			// Hand back to whatever the controller last painted —
			// listening/thinking/playing ring mid-turn, all-off when idle.
			expire()
		} else {
			clearLeds(lc)
		}
	})
	vc.mu.Unlock()
}
