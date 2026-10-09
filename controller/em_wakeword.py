"""
em_wakeword.py — turning the wake word off from Home Assistant (#286)

Three controls, and this module owns the policy between the first two:

- The MIC MUTE BUTTON on the Echo cuts the microphones in hardware. The
  controller only learns it (`mute_state`) and reports it to HA as the
  read-only "Microphone Muted" sensor (#438).
- HA's WAKE WORD PICKER turns detection off ("No wake word") or back on.
  The microphone stays live, so an HA-initiated `start_conversation` or
  `ask_question` still listens.
- The media player's mute is the SPEAKER (`em_output_mute`) and has
  nothing to do with either.

The button and the picker are independent: neither ever moves the other.
The one interaction is the stream: the device restarts its own wake stream
on unmute, so with the wake word off `on_mic_mute` takes it back down.
"""

from __future__ import annotations

from typing import NamedTuple


class Transition(NamedTuple):
    # Wake word state after this event — True means listening.
    enabled: bool
    # Send mic_stop — take the wake stream down.
    stop_stream: bool
    # Send mic_start — bring the wake stream back.
    start_stream: bool
    # The state changed, so it is recorded and the dashboard told.
    changed: bool


def requested_on(requested: list[str], model_id: str) -> bool | None:
    """
    What a VoiceAssistantSetConfiguration asks for: True for on, False for
    off, None to decline.

    Read by membership because HA sends the union of its two pickers ("Wake
    word" and "Wake word 2"). A list naming only wake words we don't have is
    declined rather than read as off; HA's re-read snaps the picker back.
    """
    if not requested:
        return False
    if model_id in requested:
        return True
    return None


def decline_off(*, want: bool, listening_locally: bool, device_can: bool) -> bool:
    """
    Whether to refuse HA's "No wake word" for this Echo.

    A privately listening Echo detects its own wake word, opens a session and
    starts sending before the controller can close it. Firmware announcing
    `wake_word_off` stops at the crossing; older firmware cannot, so off is
    declined there and HA's re-read snaps the picker back. Turning it on is
    never refused, and an Echo streaming to the controller is stopped here.
    """
    return not want and listening_locally and not device_can


def stored_off_unsupported(*, stored: bool, listening_locally: bool,
                           device_can: bool) -> bool:
    """
    Whether a STORED "No wake word" has to be given up because this firmware
    cannot honour it (#776).

    `decline_off` is the same rule applied to a request arriving now. This is
    its mirror for a choice already in the database, which nothing ever
    revisited: the device keeps opening a session on every wake and the
    controller closes each one, while Home Assistant reads "No wake word" the
    whole time. Reaching this needs the firmware to go backwards after the
    picker was set — a slot rollback, or a downgrade after #552.

    Returns True to turn the stored state back ON and report it, which is the
    same outcome as a declined request. Clearing rather than merely reporting
    is the point: keeping the stored value means the same false state returns
    on every connect after the next upgrade, and nothing self-heals.

    An Echo STREAMING to the controller with the wake word off is honouring
    it — the controller stops that stream itself — so it is never reverted.
    Only firmware that cannot stop at the crossing is.
    """
    return stored and listening_locally and not device_can


def on_request(*, want: bool, enabled: bool, mic_muted: bool) -> Transition:
    """HA's picker asked for `want`, with the wake word `enabled` and the mic
    mute button `mic_muted`."""
    if want == enabled:
        return Transition(enabled=enabled, stop_stream=False, start_stream=False, changed=False)
    # While mic-muted the device owns the stream: it refuses mic_start and
    # has already stopped it, so there is nothing to send either way.
    if want:
        return Transition(enabled=True, stop_stream=False, start_stream=not mic_muted, changed=True)
    return Transition(enabled=False, stop_stream=not mic_muted, start_stream=False, changed=True)


def on_mic_mute(*, was_muted: bool, now_muted: bool, enabled: bool) -> bool:
    """Whether to send mic_stop after a `mute_state` report. `was_muted` is
    remembered across connections, so a reconnect's re-report is not a press."""
    return was_muted and not now_muted and not enabled


def wake_allowed(*, mic_muted: bool, enabled: bool) -> bool:
    """
    Whether a wake-word crossing may start a turn, or interrupt one.

    One rule for both, and no parameter for WHERE the wake word is detected:
    an Echo that detects its own stops at the crossing (`onWakeCrossing` in
    device/cmd/server.go, which runs before a session can open or a barge be
    reported), and the controller asks this for the wakes and barges it
    scores. The two listening modes must not differ (Wil, 2026-10-05).
    """
    return enabled and not mic_muted


def stray_stream(*, mic_muted: bool, enabled: bool) -> bool:
    """
    Whether wake-stream frames arriving now are a stream that should not be up.

    With the wake word off the wake stream is down, so frames reaching the
    wake listener mean something else left one running, and the Echo is
    sending the room to the controller under "No wake word". Seen on
    2026-10-05: an Echo listening privately restarts its own local stream
    after a turn, and switching it to "On the controller" turned that into a
    network stream nobody stopped for 31 seconds. While muted the Echo sends
    nothing and owns its stream, so there is nothing to stop.
    """
    return not enabled and not mic_muted


def follow_up_needs_turn_stream(*, private: bool, enabled: bool) -> bool:
    """
    Whether a follow-up question needs its own bounded turn stream.

    A privately listening Echo has no wake stream to reuse. Nor does one
    scored here with the wake word off: its wake stream is down and
    `mic_start` is skipped while it is off, so reusing it gave the follow-up
    no microphone at all.
    """
    return private or not enabled
