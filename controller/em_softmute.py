"""
em_softmute.py — the Home Assistant soft mute, as a pure decision (#286)

Two mutes, deliberately different:

- The HARD mute is the button. It cuts the ADC in hardware, the device
  refuses every `mic_start` while it holds, and no software can clear it.
  The controller only ever LEARNS it, from `mute_state`.
- The SOFT mute is a switch HA sets. It stops the controller acting on the
  wake word and takes the continuous wake stream down, and nothing more. It
  never touches the hardware path, so it cannot enable anything that is not
  already enabled by default — which is what makes exposing it to HA safe.

The rules, and why each is here rather than in `em_controller`:

- The button always wins, in both directions. A hard-mute transition clears
  the soft mute: whoever pressed unmute expects the wake word to work, and
  whoever pressed mute has a stronger mute than the soft one.
- A reconnect is not a press. The device re-sends `mute_state` on every
  (re)connect, and a `Device` is rebuilt per connection, so the caller must
  compare against the hard state it REMEMBERS across connections — not the
  fresh object's default — or a Dot that dropped off Wi-Fi mid-film would
  come back un-soft-muted.
- The soft mute never sends `mic_start` to a hard-muted device. The device
  refuses it, and restarts the stream itself on unmute.
- The button never moves the stream from here either; the device stops and
  restarts its own stream on mute/unmute (`cmd/server.go`), and a controller
  command racing that is how a stream ends up stuck.

Like `em_button`, this is split out because the test suite does not import
`em_controller`, and this is policy that deserves coverage.
"""

from __future__ import annotations

from typing import NamedTuple


class Transition(NamedTuple):
    # Soft-mute state after this event.
    soft: bool
    # Send mic_stop — take the wake stream down.
    stop_stream: bool
    # Send mic_start — bring the wake stream back.
    start_stream: bool
    # The soft state changed, so HA needs a SwitchStateResponse.
    changed: bool


def on_switch(*, want: bool, soft: bool, hard: bool) -> Transition:
    """
    HA set the switch to `want`, with the soft mute currently `soft` and the
    hard mute currently `hard`.
    """
    if want == soft:
        return Transition(soft=soft, stop_stream=False, start_stream=False, changed=False)
    if want:
        # The device already stopped its stream if it is hard muted.
        return Transition(soft=True, stop_stream=not hard, start_stream=False, changed=True)
    return Transition(soft=False, stop_stream=False, start_stream=not hard, changed=True)


def on_hard_mute(*, hard_before: bool, hard_now: bool, soft: bool) -> Transition:
    """
    The device reported `mute_state` as `hard_now`; `hard_before` is what the
    controller remembered across connections.
    """
    if hard_now == hard_before or not soft:
        return Transition(soft=soft, stop_stream=False, start_stream=False, changed=False)
    return Transition(soft=False, stop_stream=False, start_stream=False, changed=True)


def wake_allowed(*, hard: bool, soft: bool) -> bool:
    """Whether a wake-word crossing may start a turn."""
    return not (hard or soft)
