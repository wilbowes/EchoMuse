"""Idle LED indicator policy, independent of ESPHome and controller imports.

The controller renews the 60-second device lease every 30 seconds while HA is
connected. The device renders and expires every pattern, including solid
colours, so losing HA or the controller leaves no permanent indicator.
"""
import asyncio
import colorsys
from dataclasses import dataclass, replace
import json
import logging
import math
import time

LIGHT_KEY = 6  # Append-only; key 4 belongs to the entities added by #552.
LEASE_SECONDS = 60
RENEW_SECONDS = 30
# Meter is intentionally absent: firmware measures the voice-response channel
# before mixing in music (pcm_speaker.go), so it cannot reliably visualise
# music as an idle HA effect. Voice turns retain their existing meter animation.
EFFECTS = ('None', 'Spin', 'Slow spin', 'Rotate', 'Pulse', 'Breathe', 'Rainbow')
log = logging.getLogger('echomuse.esphome.light')


def unit(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Light values must be finite')
    return min(1.0, max(0.0, value))


@dataclass(frozen=True)
class RingState:
    state: bool = False
    brightness: float = 1.0
    color_brightness: float = 1.0
    red: float = 1.0
    green: float = 1.0
    blue: float = 1.0
    effect: str = 'None'

    def update(self, **changes):
        for field in ('brightness', 'color_brightness', 'red', 'green', 'blue'):
            if field in changes:
                changes[field] = unit(changes[field])
        if 'effect' in changes:
            changes['effect'] = changes['effect'] or 'None'
            if changes['effect'] not in EFFECTS:
                raise ValueError('Unknown LED ring effect')
        return replace(self, **changes)

    def animation(self, ttl=LEASE_SECONDS):
        if not self.state:
            return {'pattern': 'off', 'listening': False}
        if ttl <= 0:
            raise ValueError('An on pattern must have a finite positive TTL')
        scale = 255 * self.brightness * self.color_brightness
        rgb = [round(getattr(self, c) * scale) for c in ('red', 'green', 'blue')]
        spec = {'pattern': 'solid', 'colors': [rgb], 'listening': False, 'ttlSec': ttl}
        if self.effect in ('Spin', 'Slow spin'):
            spec.update(pattern='spin', colors=[rgb, [round(c * 0.2) for c in rgb]],
                        periodMs=100 if self.effect == 'Spin' else 250)
        elif self.effect == 'Rotate':
            spec.update(pattern='rotate', colors=[rgb] * 3 + [[0, 0, 0]] * 9,
                        periodMs=120)
        elif self.effect in ('Pulse', 'Breathe'):
            spec.update(pattern='pulse', periodMs=1200 if self.effect == 'Pulse' else 3000)
        elif self.effect == 'Rainbow':
            palette = [[round(c * scale) for c in colorsys.hsv_to_rgb(i / 12, 1, 1)]
                       for i in range(12)]
            spec.update(pattern='rotate', colors=palette, periodMs=120)
        return spec


class RingLight:
    def __init__(self, *, clock=time.monotonic, on_change=None):
        self.state = RingState()
        self.sender = None
        self.ready = lambda: True
        self.on_change = on_change
        self.clock = clock
        self.expires_at = None
        self.revision = 0
        self.lock = asyncio.Lock()
        self.suspended = False
        self.overlay_revision = 0
        self._expiry_handle = None
        self._restore_task = None
        self._renew_task = None
        self.connected = False

    def _cancel_renewal(self):
        if self._renew_task is not None:
            self._renew_task.cancel()
            self._renew_task = None

    def set_connected(self, connected):
        """Renew only for the active HA connection; let an outage expire."""
        if self.connected == connected:
            return
        # Invalidate work queued or in flight for the previous connection.
        self.revision += 1
        self.connected = connected
        self._cancel_renewal()
        if connected:
            self._cancel_expiry()
            self._expire()
            self._start_renewal(immediate=True)
            self.schedule_restore()

    def _lease(self):
        self._cancel_expiry()
        self.expires_at = self.clock() + LEASE_SECONDS if self.state.state else None
        if self.state.state:
            self._expiry_handle = asyncio.get_running_loop().call_later(
                LEASE_SECONDS, self._expire)

    async def renew(self):
        revision = self.revision
        async with self.lock:
            if revision != self.revision or not self.connected or not self.state.state:
                return
            # Preserve the desired setting through even a long status overlay.
            # Only the status owner can restore the physical ring afterwards.
            if not self.suspended and self.ready():
                await self._write(self.state)
            if revision == self.revision:
                self._lease()

    def _start_renewal(self, *, immediate=False):
        if not self.connected or not self.state.state or self._renew_task is not None:
            return

        async def keep_alive():
            if not immediate:
                await asyncio.sleep(RENEW_SECONDS)
            while self.connected and self.state.state:
                try:
                    await self.renew()
                except Exception:
                    log.exception('Could not renew idle LED ring setting')
                await asyncio.sleep(RENEW_SECONDS)

        self._renew_task = asyncio.create_task(keep_alive())

    def _cancel_restore(self):
        if self._restore_task is not None:
            self._restore_task.cancel()
            self._restore_task = None

    def _cancel_expiry(self):
        if self._expiry_handle is not None:
            self._expiry_handle.cancel()
            self._expiry_handle = None

    def _publish(self):
        if self.on_change is not None:
            self.on_change(self.state)

    def _expire(self):
        """Mirror the device's deadline without painting over a status ring."""
        self._expiry_handle = None
        if self.expires_at is None:
            return
        remaining = self.expires_at - self.clock()
        if remaining > 0:
            self._expiry_handle = asyncio.get_running_loop().call_later(remaining, self._expire)
            return
        self.expires_at = None
        self.state = replace(self.state, state=False)
        self._cancel_renewal()
        self._publish()

    def suspend(self):
        """Status LEDs take the physical ring; HA retains its desired setting."""
        self.overlay_revision += 1
        self.suspended = True
        self._cancel_restore()

    def release(self):
        """A device disconnect/reconnect or server shutdown forgets the lease."""
        self._cancel_restore()
        self._cancel_renewal()
        self._cancel_expiry()
        self.connected = False
        self.expires_at = None
        self.suspended = False
        self.revision += 1
        self.state = replace(self.state, state=False)

    async def _write(self, state, ttl=LEASE_SECONDS):
        if self.sender is None:
            raise RuntimeError('LED device is disconnected')
        if not self.connected:
            raise RuntimeError('Home Assistant is disconnected')
        if not self.ready():
            raise RuntimeError('LED ring is in use by voice, timer or mute')
        await self.sender(state.animation(ttl))

    async def command(self, **changes):
        revision = self.revision
        async with self.lock:
            if revision != self.revision:
                raise RuntimeError('LED device connection changed')
            proposed = self.state.update(**changes)
            if self.sender is None:
                raise RuntimeError('LED device is disconnected')
            if not self.connected:
                raise RuntimeError('Home Assistant is disconnected')
            # Empty ESPHome commands do not renew a lease.
            if not changes:
                return
            if not self.suspended:
                await self._write(proposed)
            if revision != self.revision:
                return
            self.state = proposed
            self._lease()
            if proposed.state:
                self._start_renewal()
            else:
                self._cancel_renewal()

    async def restore(self):
        """Resume the requested HA setting once status LEDs release the ring."""
        async with self.lock:
            if not self.suspended:
                return True
            if not self.connected:
                # Keep the pending outcome delay across an HA reconnect.
                # Once the lease expires there is no work left to retry.
                return not self.state.state
            if not self.ready():
                return False
            revision, overlay = self.revision, self.overlay_revision
            if self.state.state:
                await self._write(self.state)
            # An off/expired lease has nothing to restore. The status owner's
            # cleanup or its own TTL clears the ring without a competing off.
            if revision == self.revision and overlay == self.overlay_revision:
                self._lease()
                self.suspended = False
                return True
            return False

    def schedule_restore(self, delay=0):
        if not self.connected or not self.suspended or self._restore_task is not None:
            return

        async def resume_when_idle():
            try:
                # Cleanup may still own voice_lock. Outcome cues keep their
                # full TTL before a manual light can return.
                await asyncio.sleep(delay)
                while not await self.restore():
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Could not restore idle LED ring setting')

        task = asyncio.create_task(resume_when_idle())
        self._restore_task = task

        def finished(done):
            if self._restore_task is done:
                self._restore_task = None

        task.add_done_callback(finished)


async def send_animation_to_device(device, animation):
    """Use the existing firmware renderer and its mute/volume suppressions."""
    if not device.led_anim_capable:
        raise RuntimeError('Device does not support LED animations')
    if device.voice_lock.locked() or device.timer_alarm_ringing or device.muted:
        raise RuntimeError('LED ring is in use by voice, timer or mute')
    # RingLight serialises light writes with its own lock. voice_lock is also
    # the wake/button paths' "turn running" signal and must stay free here.
    # Device.send_control swallows errors; do not acknowledge a failed socket
    # write as a successful light change.
    await device.control_ws.send(json.dumps({'type': 'led_anim', 'anim': animation}))
