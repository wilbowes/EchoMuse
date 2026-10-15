"""
What a device has installed, brought back in line when it connects.

A device arriving is the one moment we know what it has: its wake word
assets, start script, controller address list and (on Android) the debloat
payload are checked here, debounced per device. An emOS device's previous
boot is examined for a crash first, and its update trial confirmed, neither
debounced.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import em_crashlog
import em_emos_ota
import em_events
import em_oww_sync
import em_payloads
import em_shell
import em_state

log = logging.getLogger("echomuse.api")


# How long a device is left alone after a connect-time reconcile. Devices on
# this fleet reconnect constantly — a data-plane blip, an OTA, a controller
# restart — and none of those change what is installed on the device, so
# without a debounce the shell plane would carry three round trips every time.
# Fifteen minutes is chosen against how the payloads actually change: they
# change when someone deploys a controller or edits a config, which is minutes
# to days apart, never seconds.
DEBOUNCE_S = 15 * 60


# device_id -> monotonic time of the last connect-time reconcile.
stamps: dict[str, float] = {}


def _due(device_id: str, now: float,
                   debounce_s: float = DEBOUNCE_S) -> bool:
    """Whether this device's connect-time reconcile should run, and claim it."""
    last = stamps.get(device_id)
    if last is not None and (now - last) < debounce_s:
        return False
    # Stamped BEFORE the work rather than after: the run takes shell round
    # trips and possibly a multi-megabyte push, and a device that reconnects
    # mid-run must not start a second one against the same shell plane.
    stamps[device_id] = now
    return True


async def _collect_crash_log(live, device_id: str) -> None:
    """
    Report an emOS device's previous boot if it did not end cleanly.

    emOS saves the ram console on every boot (em_crashlog explains how a crash
    is told from a restart). A marker on the device records which copy has been
    handled, so each boot is examined once however often the device reconnects,
    and a controller restart does not report the same crash twice. The marker
    is written only after a successful read, so a device that did not answer
    is asked again on its next connect.
    """
    kmsg, seen = em_crashlog.KMSG_PATH, em_crashlog.SEEN_PATH
    probe = ""
    for attempt in range(2):
        probe = await em_shell.run(
            live, f"busybox md5sum {kmsg} 2>/dev/null; cat {seen} 2>/dev/null; "
                  f"echo {em_shell.OK_MARK}")
        if em_shell.OK_MARK in probe:
            break
        if attempt == 0:
            await asyncio.sleep(em_shell.CONNECT_RETRY_S)
            if em_state.devices.get(device_id) is not live:
                return
    else:
        log.info(f"[api] [{device_id}] crash log: no answer from the device")
        return
    m = re.search(r"\b([0-9a-f]{32})\s+" + re.escape(kmsg), probe)
    if not m:
        return  # nothing saved: a cold boot, or init that predates the copy
    md5 = m.group(1)
    if probe.count(md5) > 1:
        return  # this copy was already handled
    await asyncio.sleep(1.0)  # let the probe's shell session close
    out = await em_shell.run(live, f"cat {kmsg}; echo {em_shell.OK_MARK}", timeout=60.0)
    if em_shell.OK_MARK not in out:
        log.info(f"[api] [{device_id}] crash log: read incomplete, will retry "
                 f"on the next connect")
        return
    msg = em_crashlog.summarise(out[:out.rindex(em_shell.OK_MARK)])
    if msg:
        log.warning(f"[api] [{device_id}] previous boot did not end cleanly — "
                    f"kernel log saved to the device's log events")
        await em_events.push_log(device_id, "error", "kernel", msg)
    await asyncio.sleep(1.0)
    await em_shell.run(live, f"echo {md5} > {seen}")


async def on_connect(device_id: str, live) -> None:
    """
    Bring a freshly-connected device's three installed payloads back in line.

    A device arriving is the one moment we know what it has, and until
    2026-09-02 nothing used it: `em_oww_sync.reconcile` ran here but returned
    early unless the device scored locally and then checked only the SELECTED
    classifier, while `em_payloads.sync_start_script` and `em_payloads.sync_debloat` ran ONLY inside
    an OTA or from the Maintenance button. So a device already on the latest
    firmware never received a payload change at all — Office sat without three
    of the four stock classifiers for a fortnight with every panel calling it
    healthy.

    Three rules:

    - **Sequential, never gathered.** All three talk to the same device over
      the same shell plane; running them concurrently contends for one session
      for no gain, since none of them is on the critical path of anything.
    - **One failure must not skip the other two.** They are unrelated payloads
      and a device with a stale debloat list should still get its wake word
      models. Each is best-effort in its own right, and this only stops them
      taking each other down.
    - **Debounced per device** (`DEBOUNCE_S`), because reconnects are
      routine on this fleet and the payloads are not.

    Runs as a background task off the connect handler: nothing about the
    handshake should wait on a shell round trip over a link measured at 5-7%
    packet loss.

    An emOS device's crash log is checked first and is NOT debounced: a device
    that crashed and came back inside the window is exactly the one to look
    at, and the on-device marker already makes a repeat check one round trip.
    """
    if not live.android_userspace:
        try:
            await _collect_crash_log(live, device_id)
        except Exception as e:
            log.warning(f"[api] [{device_id}] crash log check failed ({e})")
        # Not debounced either: an image on trial has TRIAL_SECS to be
        # confirmed, and this is what confirms it after a controller restart.
        try:
            await em_emos_ota.status_on_connect(live, device_id)
        except Exception as e:
            log.warning(f"[api] [{device_id}] emOS status check failed ({e})")
    if not _due(device_id, time.monotonic()):
        return

    # Held as callables, not coroutines: building all three up front and
    # abandoning two of them leaves un-awaited coroutines to warn about later.
    steps = [
        ("oww assets", lambda: em_oww_sync.reconcile(device_id, live)),
        ("start script", lambda: em_payloads.sync_start_script(live, device_id)),
        ("controller address", lambda: em_payloads.sync_endpoints(live, device_id)),
    ]
    # The debloat payload is Android-only: a pm-hide list and a Magisk
    # service.d script. emOS has neither a package manager nor Magisk, so
    # pushing it there spends a shell round trip to run `pm hide` against
    # nothing and leave a boot script no init will read. Gated on the device
    # having POSITIVELY said it is on emOS — see Device.android_userspace for
    # why absence keeps today's behaviour.
    if live.android_userspace:
        steps.append(("debloat", lambda: em_payloads.sync_debloat(live, device_id)))
    for name, make in steps:
        # Re-read each time, and compare IDENTITY rather than presence: these
        # take seconds, and a device that dropped and redialled part-way
        # through is a different object in the registry. The one we are
        # holding then has a shell plane nobody is on the other end of, so
        # "still connected" would be true of the registry and false of us.
        if em_state.devices.get(device_id) is not live:
            log.info(f"[api] [{device_id}] reconcile: device went away — "
                     f"stopping before {name}")
            return
        try:
            await make()
        except Exception as e:
            log.warning(f"[api] [{device_id}] reconcile: {name} failed ({e}) "
                        f"— continuing with the rest")


def forget(device_id: str) -> None:
    """
    Drop a device's debounce stamp, so its next connect reconciles immediately.

    Called when a device is deleted: a re-added device is exactly the one whose
    payloads are least likely to be right, and it would otherwise inherit the
    silence of the row that was removed.
    """
    stamps.pop(device_id, None)
