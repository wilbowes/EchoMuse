"""
Home Assistant's media commands take effect in the order they were sent.

em_esphome starts each MediaPlayerCommandRequest as its own task, and the
commands yield partway: play() awaits stop(), which sends the flush before
tearing the old feed down. A pause arriving in that window saw PLAYING, found
no feed left to halt, set PAUSED and returned, and then play() started the new
track: the user's pause was lost and the music played. Each command now holds
the session's command lock.
"""

import asyncio

import em_player
from em_player import PAUSED, PLAYING, IDLE

from test_player import FakeDevice, StubSession, _wire


class SlowLinkDevice(FakeDevice):
    """A control send that yields, as a real one does on a busy link."""

    async def send_control(self, msg: dict):
        await asyncio.sleep(0.01)
        self.control_msgs.append(msg)


def setup_function(_fn):
    em_player._sessions.clear()
    em_player._notify_state = None
    em_player.DRAIN_FUDGE_S = 0.0


async def _playing(device_id="office"):
    device = SlowLinkDevice(device_id)
    _wire(device)
    s = StubSession(device_id, periods=2, endless=True)
    em_player._sessions[device_id] = s
    await s.play("http://radio/old")
    await asyncio.sleep(0.05)
    assert s.state == PLAYING
    return s


async def _send(*commands):
    # Exactly how em_esphome dispatches: one task per command, back to back.
    tasks = [asyncio.create_task(c) for c in commands]
    await asyncio.gather(*tasks)
    await asyncio.sleep(0.05)


def test_play_then_pause_ends_paused():
    async def main():
        s = await _playing()
        await _send(em_player.play("office", "http://radio/new"),
                    em_player.pause("office"))
        return s
    s = asyncio.run(main())
    assert s.state == PAUSED
    assert s.url == "http://radio/new"
    assert s._task is None or s._task.done()


def test_pause_then_play_ends_playing_the_new_track():
    async def main():
        s = await _playing()
        await _send(em_player.pause("office"),
                    em_player.play("office", "http://radio/new"))
        state, url = s.state, s.url
        await s.stop()
        return state, url
    state, url = asyncio.run(main())
    assert (state, url) == (PLAYING, "http://radio/new")


def test_play_then_stop_ends_stopped():
    async def main():
        s = await _playing()
        await _send(em_player.play("office", "http://radio/new"),
                    em_player.stop("office"))
        return s
    s = asyncio.run(main())
    assert s.state == IDLE
    assert s._task is None or s._task.done()


def test_devices_do_not_wait_on_each_other():
    async def main():
        a = await _playing("office")
        b = await _playing("lounge")
        # A slow command on one device must not hold the other's.
        lock = a._command_lock
        await lock.acquire()
        try:
            await asyncio.wait_for(em_player.pause("lounge"), 1)
        finally:
            lock.release()
        state = b.state
        await a.stop()
        return state
    assert asyncio.run(main()) == PAUSED
