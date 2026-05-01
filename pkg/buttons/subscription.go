package buttons

import (
	"context"
)

type ButtonClickCallback func(event ButtonClickEvent)

type ButtonClickEvent struct {
	Button    Button    `json:"button"`
	ClickType ClickType `json:"clickType"`
	Down      bool      `json:"down"`
}

type EventSubscription struct {
	cancel context.CancelFunc
}

func NewEventSubscription(cancelFunc context.CancelFunc) *EventSubscription {
	return &EventSubscription{
		cancel: cancelFunc,
	}
}

func (e *EventSubscription) Cancel() {
	e.cancel()
}
