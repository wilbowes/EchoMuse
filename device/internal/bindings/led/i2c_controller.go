package led

import (
	"bytes"
	"sync"

	"github.com/wilbowes/EchoMuse/pkg/led"
	"os"
	"os/exec"
)

// i2C device that sets the current led
const ledCurrentPath = "/sys/devices/soc/11007000.i2c/i2c-0/0-003f/led_current"

// i2C device that seems to control brightness
const privacyBrightnessPath = "/sys/devices/soc/10010000.keypad/amz_privacy/privacy_brightness"

// i2C device that controls the actual LEDs
const ledFrame = "/sys/devices/soc/11007000.i2c/i2c-0/0-003f/frame"

// file permission we need to access the i2C device
const perm = os.FileMode(0644)

type I2CController struct {
	// mu serialises writes to led.Leds, which is a mutable package global written
	// from multiple goroutines (mic direction callback, control plane, button
	// handler, volume timer). Without this, concurrent SetLEDs calls produce
	// torn frames written to the i2C device.
	mu sync.Mutex
}

func (i *I2CController) Init() error {

	// Initialize the LED i2C device in order for us to control it
	ledCurrentPacket := []byte("3")
	privacyBrightnessPacket := []byte{48, 0}

	err := os.WriteFile(ledCurrentPath, ledCurrentPacket, perm)
	if err != nil {
		return err
	}

        // privacy_brightness may not exist on all devices — ignore error
        _ = os.WriteFile(privacyBrightnessPath, privacyBrightnessPacket, perm)

	//if err = os.WriteFile(privacyBrightnessPath, privacyBrightnessPacket, perm); err != nil {
	//	return err
	//}

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
//	return os.WriteFile(ledFrame, targetColor.Bytes(), perm)
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
    return os.WriteFile(ledFrame, targetColor.Bytes(), perm)
}

func NewDefaultController() (led.Controller, error) {
	controller := &I2CController{}

	if err := controller.Init(); err != nil {
		return nil, err
	}

	return controller, nil
}
