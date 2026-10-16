package server

import (
	"log"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/board"
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

	// volumeBoot is the level before the controller seeds the stored one:
	// what Init used to leave the DAC at, and so what this read back.
	volumeBoot = 100
	numLEDs    = 12
)

// radarVolumeSteps are stock Radar's 30 button steps, as device levels: each
// Alexa step's value from VolumeCurves.xml (music row), through the level
// table stock's /system/bin/mixer turns a value into (Mixer_AlgoRampGain) —
// the same law as ours, 0.5dB per step with 127 = 0dB. Stock has an LED
// animation for each (volume_step-01 to -30). They replace the 4dB steps
// above the -40dB floor on Radar: 30 presses from -62dB to unity where those
// gave 10, so a press moves as far as it does on a stock Echo. Steps are
// finer in the middle and wider at the bottom, as stock's are. Controller
// side, em_volume reads the HA slider on the same table.
var radarVolumeSteps = []int{
	3, 7, 11, 17, 27, 38, 43, 49, 55, 61,
	67, 71, 73, 77, 81, 83, 87, 91, 95, 97,
	99, 103, 107, 111, 115, 117, 119, 123, 125, 127,
}

type volumeController struct {
	mu    sync.Mutex
	level int
	// steps is the board's own button ladder (radarVolumeSteps); nil keeps
	// the volumeStep band above volumeButtonFloor.
	steps          []int
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
	if board.IDOf(board.Current()) == "radar" {
		vc.steps = radarVolumeSteps
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
	level, steps := vc.level, vc.steps
	vc.mu.Unlock()
	if steps != nil {
		return vc.Set(stepAbove(steps, level), true)
	}
	return vc.Set(clampToButtonBand(level+volumeStep), true)
}

// StepDown decreases volume by one step, within the button band.
func (vc *volumeController) StepDown() bool {
	vc.mu.Lock()
	level, steps := vc.level, vc.steps
	vc.mu.Unlock()
	if steps != nil {
		return vc.Set(stepBelow(steps, level), true)
	}
	return vc.Set(clampToButtonBand(level-volumeStep), true)
}

// stepAbove is the first step above level, or the top step. A level between
// steps (HA can put it anywhere) moves to the next step up, so one press
// always lands on the ladder.
func stepAbove(steps []int, level int) int {
	for _, s := range steps {
		if s > level {
			return s
		}
	}
	return steps[len(steps)-1]
}

// stepBelow is the last step below level, or the bottom step: like the
// floor in clampToButtonBand, silencing is the mute button's job.
func stepBelow(steps []int, level int) int {
	for i := len(steps) - 1; i >= 0; i-- {
		if steps[i] < level {
			return steps[i]
		}
	}
	return steps[0]
}

// stepIndex is how many steps are at or below level: 1..len(steps) on the
// ladder, 0 below its bottom.
func stepIndex(steps []int, level int) int {
	n := 0
	for _, s := range steps {
		if s <= level {
			n++
		}
	}
	return n
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

// stepArc is the volume arc for a stepped ladder, in half-LEDs per LED:
// 2 full, 1 half-bright, 0 off. 30 steps over 12 LEDs is two and a half
// steps to an LED, so lighting whole LEDs only moves the ring every second
// or third press. The LED a step has only partly reached glows at half
// brightness, so the ring moves on nearly every press: the arc is 2*n
// half-LEDs, a step rounds up to the next, and the top step fills it. A
// level below the ladder's bottom (HA can put it there) still shows one
// half-LED, so a quiet device does not read as off.
func stepArc(steps []int, level, n int) []int {
	half := (stepIndex(steps, level)*2*n + len(steps) - 1) / len(steps)
	if half < 1 {
		half = 1
	}
	if half > 2*n {
		half = 2 * n
	}
	out := make([]int, n)
	for i := range out {
		out[i] = min(max(half-2*i, 0), 2)
	}
	return out
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
	// Whole LEDs in units of two: 2 is full cyan, 1 is the half-bright
	// LED. The ladder's arc is in half-LEDs; the band's stays whole.
	units := make([]int, numLEDs)
	for i := 0; i < lit; i++ {
		units[i] = 2
	}
	if vc.steps != nil {
		units = stepArc(vc.steps, level, numLEDs)
	}
	leds := make([]led.Led, numLEDs)
	for i := 0; i < numLEDs; i++ {
		leds[i] = led.Led{ID: i, R: 0, G: uint8(100 * units[i]), B: uint8(100 * units[i])} // cyan, 200 at full
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
