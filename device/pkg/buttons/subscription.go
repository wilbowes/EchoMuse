package buttons

import (
	"context"
)

type ButtonClickCallback func(event ButtonClickEvent)

type ButtonClickEvent struct {
	Button    Button    `json:"button"`
	ClickType ClickType `json:"clickType"`
	Down      bool      `json:"down"`
	// HeldMs is how long the button was held, set on RELEASE only (0 on the
	// press). It exists so the controller can tell a tap from a hold without
	// timing the two messages itself — the link has been measured with RTT
	// excursions past 1600ms, which would make controller-side timing of a
	// ~750ms gesture report noise as intent.
	HeldMs int64 `json:"heldMs,omitempty"`
	// GapMs is the time since the previous press of the same button, set on
	// RELEASE (0 on a press and on the first tap of a burst). It exists for
	// the same reason as HeldMs: the multi-tap window has to measure how fast
	// the user tapped, and arrival times carry the link's RTT jitter (#115).
	GapMs int64 `json:"gapMs,omitempty"`
	// Muted is the mic mute state at the instant of the press. Sent so the
	// controller judges the gesture against the state it actually happened
	// in, rather than against whatever the last mute_state message left
	// behind. Set by the forwarding callback, not by the button driver.
	Muted bool `json:"muted"`
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
