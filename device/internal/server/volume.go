package server

import (
	"fmt"
	"github.com/wilbowes/EchoMuse/pkg/led"
	"log"
	"os/exec"
	"sync"
	"time"
)

const (
	volumeMin = 0

	// volumeMax is the codec's UNITY gain, not the top of the mixer control's
	// range. tinymix ctl 61 is the tlv320aic32x4 DAC digital volume: 176
	// steps of 0.5dB spanning -63.5dB..+24dB, with 0dB at index 127. The 48
	// steps above 127 apply POSITIVE digital gain to already near-full-scale
	// PCM, which saturates inside the DAC.
	//
	// Measured on hardware 2026-08-13 (1kHz at -6dBFS, recorded through the
	// mic array): THD 1.5% at 127, 2.3% at 136, 65% at 153, 89% at 170 — with
	// the output level FLAT from 153 upward, because it had already stopped
	// being able to get louder. Third harmonic rose to -1.1dB relative to the
	// fundamental, i.e. very nearly a square wave. The control run that
	// isolates it: index 170 with the source scaled down to land at the same
	// acoustic level reads 1.1%, clean — so the codec's gain stage is fine
	// and it is purely source x gain exceeding full scale.
	//
	// Stock FireOS never writes this control at all: it appears in no
	// /system binary and not once in /system/etc/audio_device.xml, which
	// leaves the DAC at its 0dB reset default and takes user volume from
	// AudioFlinger's software attenuation instead. That is why native Alexa
	// has no such distortion, and why matching it means capping here.
	//
	// Do not raise this. The lost headroom cannot be bought back from
	// Ext_Amp_Gain either — that control is inert on this board (measured
	// 0.0dB of effect across its whole 6/12/18/24dB range, while still
	// reading its new value back). "HP Driver Gain Volume" (ctl 62) is the
	// stage that does work, if more output is ever wanted.
	volumeMax = 127

	// volumeButtonFloor is the bottom of the band the PHYSICAL buttons
	// traverse. The scale is dB-linear, so index 0 is -63.5dB and roughly the
	// bottom third of the control is indistinguishable from silence; stepping
	// across it spends presses to go nowhere. Silencing the device is the
	// mute button's job, not the volume button's.
	//
	// Explicit Set() calls are deliberately NOT floored — HA's volume 0.0
	// has to still mean silent. A press from below the floor lands ON the
	// floor rather than adding a step, so one press always reaches audible.
	volumeButtonFloor = 47 // -40dB

	// volumeStep is 4dB per press: 10 presses to cross the button band.
	volumeStep    = 8
	volumeLEDSecs = 2 // how long to show volume ring
	numLEDs       = 12
)

type volumeController struct {
	mu             sync.Mutex
	level          int
	ledCtrl        func() led.Controller // getter so we handle nil during boot
	timer          *time.Timer
	displayActive  bool        // volume arc currently on the ring — see DisplayActive
	isMuted        func() bool // set after construction to avoid circular dependency
	onVolumeChange func(int)   // set after construction; called after every Set()
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
	}
	// Read initial volume from tinymix
	vc.level = vc.readFromDevice()
	log.Printf("Volume controller initialised at %d/%d", vc.level, volumeMax)
	return vc
}

// readFromDevice reads current tinymix level. Returns the midpoint of the
// button band on failure — volumeMax/2 is -32dB on this dB-linear scale,
// which is quiet enough to read as broken.
func (vc *volumeController) readFromDevice() int {
	fallback := (volumeButtonFloor + volumeMax) / 2
	out, err := exec.Command("tinymix", "-D", "0", volumeSelector).Output()
	if err != nil {
		log.Printf("Volume read failed: %v", err)
		return fallback
	}
	var l, r int
	// Output: "<volumeDisplayName>: 100 100 (range 0->175)". The control's
	// own range is 0->175; volumeMax caps us at 127 (unity) — see the
	// constant. A device that was left above the cap reads back high here
	// and the next Set() clamps it.
	if _, err := fmt.Sscanf(string(out), volumeDisplayName+": %d %d", &l, &r); err != nil {
		log.Printf("Volume parse failed: %v (output: %s)", err, out)
		return fallback
	}
	if l > volumeMax {
		l = volumeMax
	}
	return l
}

// Set applies a new volume level (0–volumeMax) and updates tinymix. showRing
// paints the cyan volume arc for the 2s display window — physical button
// presses pass true; remote sets (controller command / HA) and the boot-time
// SeedVolume pass false so the ring doesn't light when nobody is at the
// device.
func (vc *volumeController) Set(level int, showRing bool) {
	if level < volumeMin {
		level = volumeMin
	}
	if level > volumeMax {
		level = volumeMax
	}

	vc.mu.Lock()
	vc.level = level
	// Copy under the lock — SetOnVolumeChange writes this field under mu
	// from the main goroutine, and button events can fire before that
	// wiring completes (SubscribeToButton starts the evdev goroutines
	// first).
	cb := vc.onVolumeChange
	vc.mu.Unlock()

	// Apply to ALSA
	if err := exec.Command("tinymix", "-D", "0", volumeSelector,
		fmt.Sprintf("%d", level), fmt.Sprintf("%d", level)).Run(); err != nil {
		log.Printf("tinymix set failed: %v", err)
	}

	log.Printf("Volume set to %d/%d", level, volumeMax)
	if showRing {
		vc.showLEDs(level)
	}
	if cb != nil {
		cb(level)
	}
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
func (vc *volumeController) StepUp() {
	vc.mu.Lock()
	level := vc.level + volumeStep
	vc.mu.Unlock()
	vc.Set(clampToButtonBand(level), true)
}

// StepDown decreases volume by one step, within the button band.
func (vc *volumeController) StepDown() {
	vc.mu.Lock()
	level := vc.level - volumeStep
	vc.mu.Unlock()
	vc.Set(clampToButtonBand(level), true)
}

// clampToButtonBand holds a stepped level inside [volumeButtonFloor,
// volumeMax]. A device sitting below the floor — HA can put it there, and so
// can a stored level from before the cap — lands ON the floor from one press
// instead of creeping up 4dB at a time through inaudible territory.
func clampToButtonBand(level int) int {
	if level < volumeButtonFloor {
		return volumeButtonFloor
	}
	if level > volumeMax {
		return volumeMax
	}
	return level
}

// showLEDs lights N of 12 LEDs in cyan proportional to volume, then clears after 2s.
func (vc *volumeController) showLEDs(level int) {
	lc := vc.ledCtrl()
	if lc == nil {
		return
	}

	// The arc spans the BUTTON band, not the full control: over 0..volumeMax
	// the audible range crowds into the top LEDs and a press often moves
	// nothing. One LED stays lit anywhere above silence so the ring never
	// reads as "off" when the device is merely quiet.
	span := volumeMax - volumeButtonFloor
	lit := (level - volumeButtonFloor) * numLEDs / span
	if lit < 1 && level > volumeMin {
		lit = 1
	}
	if lit > numLEDs {
		lit = numLEDs
	}
	leds := make([]led.Led, numLEDs)
	for i := 0; i < numLEDs; i++ {
		if i < lit {
			leds[i] = led.Led{ID: i, R: 0, G: 200, B: 200} // cyan
		} else {
			leds[i] = led.Led{ID: i, R: 0, G: 0, B: 0}
		}
	}
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
