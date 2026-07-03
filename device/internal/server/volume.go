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
	volumeMin     = 0
	volumeMax     = 175
	volumeStep    = 17  // ~10% per press
	volumeLEDSecs = 2   // how long to show volume ring
	numLEDs       = 12
)

type volumeController struct {
	mu             sync.Mutex
	level          int
	ledCtrl        func() led.Controller // getter so we handle nil during boot
	timer          *time.Timer
	isMuted        func() bool // set after construction to avoid circular dependency
	onVolumeChange func(int)   // set after construction; called after every Set()
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

// readFromDevice reads current tinymix level. Returns volumeMax/2 on failure.
func (vc *volumeController) readFromDevice() int {
	out, err := exec.Command("tinymix", "-D", "0", "61").Output()
	if err != nil {
		log.Printf("Volume read failed: %v", err)
		return volumeMax / 2
	}
	var l, r int
	// Output: "PCM Playback Volume: 100 100 (range 0->175)"
	if _, err := fmt.Sscanf(string(out), "PCM Playback Volume: %d %d", &l, &r); err != nil {
		log.Printf("Volume parse failed: %v (output: %s)", err, out)
		return volumeMax / 2
	}
	return l
}

// Set applies a new volume level (0–175), updates tinymix and LEDs.
func (vc *volumeController) Set(level int) {
	if level < volumeMin {
		level = volumeMin
	}
	if level > volumeMax {
		level = volumeMax
	}

	vc.mu.Lock()
	vc.level = level
	vc.mu.Unlock()

	// Apply to ALSA
	if err := exec.Command("tinymix", "-D", "0", "61",
		fmt.Sprintf("%d", level), fmt.Sprintf("%d", level)).Run(); err != nil {
		log.Printf("tinymix set failed: %v", err)
	}

	log.Printf("Volume set to %d/%d", level, volumeMax)
	vc.showLEDs(level)
	if vc.onVolumeChange != nil {
		vc.onVolumeChange(level)
	}
}

// Get returns current volume level.
func (vc *volumeController) Get() int {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	return vc.level
}

// StepUp increases volume by one step.
func (vc *volumeController) StepUp() {
	vc.mu.Lock()
	level := vc.level + volumeStep
	vc.mu.Unlock()
	vc.Set(level)
}

// StepDown decreases volume by one step.
func (vc *volumeController) StepDown() {
	vc.mu.Lock()
	level := vc.level - volumeStep
	vc.mu.Unlock()
	vc.Set(level)
}

// showLEDs lights N of 12 LEDs in cyan proportional to volume, then clears after 2s.
func (vc *volumeController) showLEDs(level int) {
	lc := vc.ledCtrl()
	if lc == nil {
		return
	}

	lit := level * numLEDs / volumeMax
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
	if vc.timer != nil {
		vc.timer.Stop()
	}
	vc.timer = time.AfterFunc(volumeLEDSecs*time.Second, func() {
		if vc.isMuted != nil && vc.isMuted() {
			// Restore mute indicator — red ring
			leds := make([]led.Led, numLEDs)
			for i := 0; i < numLEDs; i++ {
				leds[i] = led.Led{ID: i, R: 180, G: 0, B: 0}
			}
			lc.SetLEDs(leds...)
		} else {
			clearLeds(lc)
		}
	})
	vc.mu.Unlock()
}
