"""
em_identify.py — "which Echo is this?": the dashboard's Identify action (#860)
==========================================================================

Flashes one Echo's ring for a few seconds so a person standing among several
can match a dashboard row to the device in front of them. Until now the only
way was to tap each one's action button and read the log.

Pure: the decision and the ring spec, with no sockets. The route in
em_routes_devices asks `decide()`, and acts only on `OK`.

The ring is shared, so Identify has to ask before it takes it:

  * MUTED. The device paints no controller frame while muted (mute is
    device-sovereign, docs/led-ring-states.md), so Identify would show nothing
    and report success. It is refused, and says why. The red mute ring is how
    that Echo is told apart anyway.
  * BUSY. A voice turn, an announcement, a reply or a ringing timer already
    owns the ring, and taking it would clear it out from under them.
  * NO led_anim. Older firmware has no device-side animator, and a controller
    streaming raw frames for several seconds is the jitter led_anim removed.
    The dashboard shows the control disabled with this reason.

The look: a violet pulse. Red is mute, orange is "no controller", cyan is the
volume arc, white is "pending approval" and amber is a ringing timer, so none
of those can be it, and a one-colour pulse needs no legend. `ttlSec` is the
dead-man that clears the ring if the controller stops talking mid-flash.
"""

from __future__ import annotations

OK = "ok"
NOT_CONNECTED = "not_connected"
NO_ANIM = "no_led_anim"
MUTED = "muted"
BUSY = "busy"

# How long the ring flashes. Long enough to look from the dashboard to the
# desk and find it, short enough that nobody is left wondering when it stops.
DURATION_S = 6

IDENTIFY_COLOUR = (150, 70, 255)

# Slow enough to read as a deliberate beacon and not a fault, which the fast
# blinks of the error cues are.
IDENTIFY_ANIM = {
    "pattern":  "pulse",
    "colors":   [list(IDENTIFY_COLOUR)],
    "periodMs": 600,
    "ttlSec":   DURATION_S,
}

# What the dashboard may say for each refusal, so the route and the button
# cannot drift apart.
MESSAGES = {
    NOT_CONNECTED: "This Echo is not connected.",
    NO_ANIM:       "This Echo's firmware cannot flash its ring on request.",
    MUTED:         "This Echo is muted, so its ring stays red.",
    BUSY:          "This Echo is busy. Try again in a moment.",
}

STATUS = {OK: 200, NOT_CONNECTED: 409, NO_ANIM: 409, MUTED: 409, BUSY: 409}


def decide(*, connected: bool, led_anim_capable: bool, muted: bool,
           turn_running: bool, speaking: bool, alarm_ringing: bool) -> str:
    """Whether Identify may take this Echo's ring now, and if not, why."""
    if not connected:
        return NOT_CONNECTED
    if not led_anim_capable:
        return NO_ANIM
    if muted:
        return MUTED
    if turn_running or speaking or alarm_ringing:
        return BUSY
    return OK
