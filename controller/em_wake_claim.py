"""
Claiming a wake for one Echo when several heard it: the arbitration slack
and hold windows, when a device-side wake was heard (its own timestamp, or
arrival less the link's latency), and the claim against em_arbiter.

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import logging
import time

import em_db as db
import em_dbwriter
import em_device
import em_esphome as esphome
import em_listen
import em_state
import em_wakelevel

log = logging.getLogger("echomuse")


def _arbitration_slack() -> float:
    return em_listen.MAX_ARB_SLACK_S


def _arbitration_hold() -> float:
    """Hold claims whenever two or more Echoes could claim; see
    em_listen.ARB_HOLD_S. One with no HA stands down first, so does not count."""
    return em_listen.arbitration_hold(
        em_listen.detector(d.listen_view, d.oww_trigger_capable)
        for d in list(em_state.devices.values()) if esphome.can_serve_turn(d.device_id)
    )


async def claim_wake(device: "em_device.Device", heard_at: float | None,
                      level: tuple[float, float] | None = None,
                      by: str = "controller",
                      over_playback: bool = False) -> str:
    """Claim the utterance for `device`; returns the winner's id.

    `level` is the wake's (level, peak) in dBFS. Its level is what the arbiter
    compares across Echoes (em_arbiter.pick), and it is logged with its
    capture time so contested wakes can be paired later; see em_wakelevel.
    `over_playback` says this Echo heard the wake over its own speaker, which
    the reading then contains, so the contest is decided on time.
    """
    hold = _arbitration_hold()
    won_by = await em_state.wake_arbiter.contest(
        device.device_id, device.wake_arb_ms / 1000.0,
        heard_at=heard_at, slack_s=_arbitration_slack(), hold_s=hold,
        level=level[0] if level is not None else None,
        gains=device.adc_gains, over_playback=over_playback,
        ringing=device.timer_alarm_ringing,
    )
    now = asyncio.get_event_loop().time()
    if hold > 0:
        ago = (now - heard_at) * 1000.0 if heard_at is not None else 0.0
        log.info(f"[{device.device_id}] arbitration (held {hold * 1000:.0f}ms): "
                 f"heard {ago:.0f}ms ago, won by {won_by} "
                 f"({em_state.wake_arbiter.last_why})")
    if level is not None:
        heard_wall = time.time() - (now - (heard_at if heard_at is not None else now))
        em_dbwriter.submit(db.log_device, device.device_id, "info", "controller", em_wakelevel.log_line(
            level[0], level[1], device.noise_floor, device.mic_gain_db,
            heard_wall, by))
    return won_by


def device_level(ev: dict) -> tuple[float, float] | None:
    """The level an Echo measured for its own wake, if its firmware sends one."""
    if ev.get("level") is None or ev.get("peak") is None:
        return None
    return ev["level"], ev["peak"]


def wake_dating(device: em_device.Device, ev: dict) -> tuple[float, str]:
    """When a device wake was heard, and how that was worked out.

    The Echo's own capture instant, mapped through its clock, survives any
    time in flight; age plus half an RTT assumes the message was not delayed,
    which is exactly when it matters. The latter is for firmware that does
    not send capturedMono, and until the first ping reply maps the clock.
    """
    t = device.device_clock.to_local(ev.get("captured_mono"), ev["arrived"])
    if t is not None:
        return t, "device clock"
    return em_listen.heard_at(ev["arrived"], ev["age_ms"], device.rtt_est.srtt), "age + RTT"


def wake_heard_at(device: em_device.Device, ev: dict) -> float:
    return wake_dating(device, ev)[0]
