package led

import (
	"bytes"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"sync"

	"github.com/wilbowes/EchoMuse/pkg/board"
	"github.com/wilbowes/EchoMuse/pkg/led"
)

// The ring driver's attributes, under its i2c client's directory. The client
// is found by driver name (pkg/board): the bus and address in its path are
// where this board happens to put it.
const (
	// LED drive current.
	ledCurrentAttr = "led_current"
	// The frame the LEDs show.
	ledFrameAttr = "frame"
	// The is31fl3236 driver animates the ring itself from boot until
	// something clears this. Android's userspace does; ours does not, so on a
	// device running our own init the kernel animation and our frames drive
	// the same LEDs and the ring visibly glitches. Reads 1 under our
	// userspace and 0 on stock.
	bootAnimationAttr = "boot_animation"
)

// file permission we need to access the i2C device
const perm = os.FileMode(0644)

type I2CController struct {
	// mu serialises writes to led.Leds, which is a mutable package global written
	// from multiple goroutines (mic direction callback, control plane, button
	// handler, volume timer). Without this, concurrent SetLEDs calls produce
	// torn frames written to the i2C device.
	mu sync.Mutex
	// dir is the ring driver's sysfs directory and frame its frame attribute,
	// set by Init.
	dir   string
	frame string
}

func (i *I2CController) Init() error {
	i.dir = board.CurrentLayout().LEDRing
	if i.dir == "" {
		return errors.New("led: ring driver not found on this board")
	}
	i.frame = filepath.Join(i.dir, ledFrameAttr)

	// Stop the kernel's boot animation before the first frame, or it keeps
	// driving the same LEDs. Best-effort: on stock Android something has
	// already cleared it, and a board without the attribute is not a failure.
	_ = os.WriteFile(filepath.Join(i.dir, bootAnimationAttr), []byte("0"), perm)

	// Initialize the LED i2C device in order for us to control it
	ledCurrentPacket := []byte("3")
	privacyBrightnessPacket := []byte{48, 0}

	err := os.WriteFile(filepath.Join(i.dir, ledCurrentAttr), ledCurrentPacket, perm)
	if err != nil {
		return err
	}

	// Brightness of the mute button's LED, where the kernel has Amazon's
	// privacy driver (privacy.go finds it by name). Best-effort.
	if d := privacyDir(); d != "" {
		_ = os.WriteFile(filepath.Join(d, "privacy_brightness"), privacyBrightnessPacket, perm)
	}

	// ledcontroller may overwrite our led config
	// solution: let android kill it
	cmd := exec.Command("stop", "ledcontroller")
	_ = cmd.Run()
	//if err = cmd.Run(); err != nil {
	//	return err
	//}
	return nil
}

func (i *I2CController) GetNumLEDs() (int, error) {
	return len(led.Leds), nil
}

//func (i *I2CController) SetLEDs(LEDs ...led.Led) error {
//	var targetColor bytes.Buffer
//	for index, curLed := range LEDs {
//		for _, targetLed := range led.Leds {
//			if curLed.ID == targetLed.ID {
//				led.Leds[index] = targetLed
//				break
//			}
//		}
//
//		targetColor.Write(led.Leds[index].BuildArgument())
//	}
//	return os.WriteFile(i.frame, targetColor.Bytes(), perm)
//}

func (i *I2CController) SetLEDs(LEDs ...led.Led) error {
	i.mu.Lock()
	defer i.mu.Unlock()
	var targetColor bytes.Buffer
    for _, curLed := range LEDs {
        for j, storedLed := range led.Leds {
            if curLed.ID == storedLed.ID {
                led.Leds[j] = curLed  // update stored with incoming
                break
            }
        }
    }
    for _, l := range led.Leds {
        targetColor.Write(l.BuildArgument())
    }
    return os.WriteFile(i.frame, targetColor.Bytes(), perm)
}

func NewDefaultController() (led.Controller, error) {
	controller := &I2CController{}

	if err := controller.Init(); err != nil {
		return nil, err
	}

	return controller, nil
}
