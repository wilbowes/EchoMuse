"""
em_button.py — what an Action-button press means, as a pure decision

Split out of `em_controller.handle_button_event` for the same reason as
`em_linkauth`: the test suite does not import `em_controller`, so this was
policy with no coverage — and it silently lost a gesture for the whole life
of the hold feature (see below).

The rule the device used to enforce, and no longer does:

    While muted, a press must not start a VOICE TURN.

Note what that does not say. It says nothing about which gestures reach Home
Assistant. The device dropped every dot press while muted
(`device/cmd/server.go`), which was right when the button meant exactly one
thing — starting a turn — and became wrong the moment a hold also meant "fire
an HA event". A hold bound to something unrelated to speech stopped working
whenever the mic was muted, with nothing on the device to connect the two.

Mute is still sovereign on the DEVICE and that is not weakened by moving this
here. The guarantee is not the button filter, it is that the device rejects
every `mic_start` while muted and the ADC is muted in hardware regardless. So
even a controller with a stale idea of the mute state cannot open the mic; the
worst it can do is start a turn that captures silence and ends `no_speech`.
Keep that rejection where it is — it is what makes this file safe.
"""

from __future__ import annotations

# A hold is forwarded to HA as the `long` event. Fires while muted: it is not
# speech, so the mute has no opinion about it.
HOLD = "hold"
# A tap forwarded to HA as an event rather than starting a turn
# (buttonSingleTapEvent). Ordered before the mute check for the same reason a
# hold is: it is not speech, so the mute has no opinion about it.
TAP_EVENT = "tap_event"
# A tap while muted. Does nothing at all.
BLOCKED = "blocked"
# A tap during an active turn cancels it.
CANCEL = "cancel"
# A tap otherwise starts one.
TURN = "turn"


def decide(
    *,
    held_ms: int,
    hold_ms: int,
    muted: bool,
    turn_active: bool,
    tap_event: bool = False,
) -> str:
    """
    Classify a dot-button RELEASE.

    `held_ms` is measured on the device and reported on release; 0 (its value
    on firmware predating the hold feature, and on any press) reads as a tap,
    so an unknown hold time can never be promoted to a gesture the user did
    not make.

    `tap_event` is buttonSingleTapEvent ANDed with `button_hold` capability
    by the caller: the event entity is only advertised for a hold-capable
    device, so ungated this would classify taps as events nothing receives —
    and make TURN and CANCEL unreachable. An inert button.
    """
    if held_ms >= hold_ms:
        return HOLD
    if tap_event:
        return TAP_EVENT
    if muted:
        return BLOCKED
    if turn_active:
        return CANCEL
    return TURN
