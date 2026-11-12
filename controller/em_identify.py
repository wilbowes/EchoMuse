"""
em_identify.py — "which Echo is this?": the dashboard's Identify action (#860)
==========================================================================

Asks one Echo to take its whole ring for DURATION_S so a person standing among
several can match a dashboard row to the device in front of them.

The ring work is the firmware's. On an `identify` message it takes a top layer
above every other ring owner (turns, replies, the mute ring, the volume arc,
the link pulse) and holds it for its own 30 seconds, so there is nothing here
to refuse for being muted or busy, no animation to choose and no dead-man to
send. The mute button's own LED is a separate GPIO and keeps showing mute.

There is no fallback. Firmware that does not announce `identify_ring` cannot do
it, and the dashboard shows the control disabled with that reason: sending
`led_anim` instead would share a layer with turns and replies, which paint
over it.

Pure: the decision, with no sockets. The route in em_routes_devices asks
`decide()`, and acts only on `OK`.
"""

from __future__ import annotations

OK = "ok"
NOT_CONNECTED = "not_connected"
NO_IDENTIFY = "no_identify_ring"

# How long the ring is held. The device owns the timer; this is only what the
# dashboard shows while it runs. Keep it equal to `identifyDuration` in
# device/internal/server/identify.go.
DURATION_S = 30

# What the dashboard may say for each refusal, so the route and the button
# cannot drift apart.
MESSAGES = {
    NOT_CONNECTED: "This Echo is not connected.",
    NO_IDENTIFY:   "This Echo's firmware cannot flash its ring on request.",
}

STATUS = {OK: 200, NOT_CONNECTED: 409, NO_IDENTIFY: 409}


def decide(*, connected: bool, identify_ring_capable: bool) -> str:
    """Whether Identify may be sent to this Echo, and if not, why."""
    if not connected:
        return NOT_CONNECTED
    if not identify_ring_capable:
        return NO_IDENTIFY
    return OK
