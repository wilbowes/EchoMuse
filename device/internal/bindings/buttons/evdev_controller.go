package buttons

import (
	"context"
	"errors"
	"fmt"
	"log"

	evdev "github.com/gvalkov/golang-evdev"
	"github.com/wilbowes/EchoMuse/internal/profile"
	"github.com/wilbowes/EchoMuse/pkg/buttons"
)

// Device paths come from the profile; a device without an action button
// leaves DotDevice empty and only the volume node is opened.

// VolumeCallback is called on volume button release with direction "up" or "down".
type VolumeCallback func(direction string)

// MuteCallback is called on mute button release.
type MuteCallback func()

type EvDevController struct {
	prof           *profile.Profile
	volumeCallback func(direction string)
	muteCallback   func()
}

// SetVolumeCallback registers a function to be called on volume button events.
// Must be called before SubscribeToButton.
func (e *EvDevController) SetVolumeCallback(cb func(direction string)) {
	e.volumeCallback = cb
}

// SetMuteCallback registers a function to be called on mute button events.
// Must be called before SubscribeToButton.
func (e *EvDevController) SetMuteCallback(cb func()) {
	e.muteCallback = cb
}

// Init prepares the button listeners.
//
// Stopping the stock button daemon is part of the profile's StopServices
// (Fire OS calls it "acebutton"; LineageOS has no equivalent), so there is
// nothing left to do here. It previously ran `stop acebutton` and returned
// the error, which made startup fail outright on any OS without that service.
func (e *EvDevController) Init() error {
	return nil
}

func (e *EvDevController) SubscribeToButton(callback buttons.ButtonClickCallback) (*buttons.EventSubscription, error) {
	if callback == nil {
		return nil, errors.New("callback can't be nil")
	}

	var dotDevice, volDevice *evdev.InputDevice
	if p := e.prof.Buttons.DotDevice; p != "" {
		d, err := evdev.Open(p)
		if err != nil {
			return nil, fmt.Errorf("open dot button %s: %w", p, err)
		}
		dotDevice = d
	}
	if p := e.prof.Buttons.VolumeDevice; p != "" {
		d, err := evdev.Open(p)
		if err != nil {
			if dotDevice != nil {
				dotDevice.Release()
			}
			return nil, fmt.Errorf("open volume button %s: %w", p, err)
		}
		volDevice = d
	}
	if dotDevice == nil && volDevice == nil {
		log.Printf("buttons: no input devices configured for %s, so no physical controls", e.prof.Name)
	}

	ctx, cancel := context.WithCancel(context.Background())
	eventSub := buttons.NewEventSubscription(cancel)

	readBtn := func(btn buttons.Button, btnDevice *evdev.InputDevice) {
		defer btnDevice.Release()

		beforeClickType := buttons.ClickType(0)
		beforeDown := false

		for {
			if ctx.Err() != nil {
				return
			}

			inputEvent, err := btnDevice.ReadOne()
			if err != nil {
				return
			}

			clickType := buttons.ClickType(inputEvent.Code)
			if inputEvent.Code != 0 {
				beforeClickType = clickType
			} else {
				clickType = beforeClickType
			}

			down := inputEvent.Value == 1
			if beforeDown == down {
				continue
			}
			beforeDown = down

			// Intercept volume events on volume device
			if btn.Type == buttons.VolumeButton && !down {
				switch clickType {
				case buttons.VolumeUpClick:
					if e.volumeCallback != nil {
						e.volumeCallback("up")
					}
				case buttons.VolumeDownClick:
					if e.volumeCallback != nil {
						e.volumeCallback("down")
					}
				}
				continue
			}

			// Intercept mute on dot device
			if btn.Type == buttons.DotButton && !down && clickType == buttons.MuteClick {
				if e.muteCallback != nil {
					e.muteCallback()
				}
				continue
			}

			callback(buttons.ButtonClickEvent{
				Button:    btn,
				ClickType: clickType,
				Down:      down,
			})
		}
	}

	if dotDevice != nil {
		go readBtn(e.GetDotButton(), dotDevice)
	}
	if volDevice != nil {
		go readBtn(e.GetVolumeButton(), volDevice)
	}

	return eventSub, nil
}

func (e *EvDevController) GetVolumeButton() buttons.Button {
	return buttons.Button{
		Type: buttons.VolumeButton,
	}
}

func (e *EvDevController) GetDotButton() buttons.Button {
	return buttons.Button{
		Type: buttons.DotButton,
	}
}

func NewButtonController(prof *profile.Profile) (*EvDevController, error) {
	controller := &EvDevController{prof: prof}
	if err := controller.Init(); err != nil {
		return nil, err
	}
	return controller, nil
}
