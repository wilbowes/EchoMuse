package buttons

import (
	"context"
	"errors"
	"os/exec"
	"time"

	evdev "github.com/gvalkov/golang-evdev"
	"github.com/wilbowes/EchoMuse/pkg/board"
	"github.com/wilbowes/EchoMuse/pkg/buttons"
)

// VolumeCallback is called on volume button release with direction "up" or "down".
type VolumeCallback func(direction string)

// MuteCallback is called on mute button release.
type MuteCallback func()

type EvDevController struct {
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

// Init stops Amazon's button daemon, which is acebutton on FireOS 5 and
// acebuttond on FireOS 6. Both are asked, since missing the one that exists
// leaves Amazon acting on every press alongside us; it fails only if neither
// stop could run.
func (e *EvDevController) Init() error {
	errA := exec.Command("stop", "acebutton").Run()
	errB := exec.Command("stop", "acebuttond").Run()
	if errA != nil && errB != nil {
		return errA
	}
	return nil
}

func (e *EvDevController) SubscribeToButton(callback buttons.ButtonClickCallback) (*buttons.EventSubscription, error) {
	if callback == nil {
		return nil, errors.New("callback can't be nil")
	}

	// The input devices are found by name (pkg/board): eventN is enumeration
	// order, and the wrong device opens without error and reads nothing.
	keys := board.CurrentLayout().Keys
	if len(keys) == 0 {
		return nil, errors.New("buttons: input devices not found on this board")
	}
	var devices []*evdev.InputDevice
	for _, path := range keys {
		d, err := evdev.Open(path)
		if err != nil {
			for _, open := range devices {
				open.Release()
			}
			return nil, err
		}
		devices = append(devices, d)
	}

	ctx, cancel := context.WithCancel(context.Background())
	eventSub := buttons.NewEventSubscription(cancel)

	read := func(dev *evdev.InputDevice) {
		defer dev.Release()
		state := newKeyState()
		for ctx.Err() == nil {
			ev, err := dev.ReadOne()
			if err != nil {
				return
			}
			// Key events only: each press is followed by an EV_SYN whose code
			// and value are both 0.
			if ev.Type != evdev.EV_KEY {
				continue
			}
			k, ok := state.handle(ev.Code, ev.Value, time.Now())
			if !ok {
				continue
			}
			if isVolume(k.Code) {
				if !k.Down && e.volumeCallback != nil {
					e.volumeCallback(map[buttons.ClickType]string{
						buttons.VolumeUpClick: "up", buttons.VolumeDownClick: "down"}[k.Code])
				}
				continue
			}
			if k.Code == buttons.MuteClick {
				if !k.Down && e.muteCallback != nil {
					e.muteCallback()
				}
				continue
			}
			callback(buttons.ButtonClickEvent{
				Button:    e.GetDotButton(),
				ClickType: k.Code,
				Down:      k.Down,
				HeldMs:    k.HeldMs,
			})
		}
	}
	for _, d := range devices {
		go read(d)
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

func NewButtonController() (*EvDevController, error) {
	controller := &EvDevController{}
	if err := controller.Init(); err != nil {
		return nil, err
	}
	return controller, nil
}
