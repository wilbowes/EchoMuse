package led

import (
	"context"
	"errors"
	"os"
	"time"
)

type Animator struct {
	ledController Controller
}

// Animator for the animations found in /system/etc/led-resources
func NewAnimator(controller Controller) *Animator {
	return &Animator{
		ledController: controller,
	}
}

func (a *Animator) GetAnimation(path string) (Animation, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return Animation{}, err
	}
	return ParseAnimation(string(body))
}

func (a *Animator) Play(animation Animation, ctx context.Context) error {
	for {
		for _, step := range animation.Steps {
			if ctx.Err() != nil {
				if errors.Is(ctx.Err(), context.Canceled) {
					return nil
				}
				return ctx.Err()
			}
			if err := a.ledController.SetLEDs(step.LedConfig...); err != nil {
				return err
			}
			time.Sleep(step.Duration)
		}

		if !animation.Looped {
			break
		}
	}
	return nil
}
