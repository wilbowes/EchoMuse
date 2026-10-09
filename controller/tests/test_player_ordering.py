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


def test_a_reconnect_mid_stream_moves_the_feed_to_the_new_device():
    # #751: a control reconnect registers a NEW Device object, so the one
    # _feed resolved at its start goes stale — its data socket is closed and
    # its config stops moving. Before the per-chunk re-resolve the feed kept
    # writing into the dead socket for the rest of the track, one "Data send
    # failed" line per period, and every EOS went the same way.
    async def main():
        old = FakeDevice("office")
        new = FakeDevice("office")
        current = [old]

        em_player.init(get_device=lambda did: current[0], notify_state=None)
        em_player._notify_state = None

        s = StubSession("office", periods=40, endless=True, gap=0.005)
        em_player._sessions["office"] = s
        await s.play("http://radio/old")
        await asyncio.sleep(0.03)

        # The power cut: the old socket dies and a replacement Device
        # registers, exactly as handle_data now sees it.
        current[0] = new
        await asyncio.sleep(0.05)

        await em_player.stop("office")
        return old, new, s

    old, new, s = asyncio.run(main())
    # Every frame of the stream after the swap went to the NEW device.
    assert len(old.data_frames) < len(new.data_frames)
    assert len(new.data_frames) > 0, "the feed never reached the new device"
    assert s.state == IDLE


def test_a_feed_whose_device_leaves_entirely_ends_and_releases_the_lock():
    # The other half of #751: with the replacement gone the getter returns
    # None, and the feed must stop rather than spin on a device that is not
    # there. The command lock must come back, or every later command queues
    # behind a feed that never finishes.
    async def main():
        device = FakeDevice("office")
        current = [device]
        em_player.init(get_device=lambda did: current[0], notify_state=None)
        em_player._notify_state = None

        s = StubSession("office", periods=40, endless=True, gap=0.005)
        em_player._sessions["office"] = s
        await s.play("http://radio/old")
        await asyncio.sleep(0.03)

        current[0] = None
        await asyncio.wait_for(s._task, 2.0)
        # And the lock is free — a following command completes rather than
        # queueing behind a feed that will never finish.
        await asyncio.wait_for(em_player.stop("office"), 2.0)
        return s

    s = asyncio.run(main())
    assert s.state == IDLE


class HalfOpenDevice(FakeDevice):
    """A device that lost power mid-stream: its data socket is half-open, so
    once `dead` is set no send on it ever returns (observed 2026-10-04)."""

    def __init__(self, device_id="office"):
        super().__init__(device_id)
        self.dead = False
        self._never = asyncio.Event()

    async def send_data(self, data: bytes):
        if self.dead:
            await self._never.wait()
        self.data_frames.append(data)


def test_a_dead_link_does_not_wedge_the_player():
    # Before: stop() awaited the EOS on the half-open socket forever while
    # holding the command lock, so every later play/pause queued behind it and
    # Home Assistant showed `playing` until the add-on restarted.
    em_player.EOS_WAIT_S = 0.05

    async def main():
        device = HalfOpenDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s
        await s.play("http://radio/old")
        await asyncio.sleep(0.05)
        device.dead = True
        # Timed rather than relied on wait_for to fail: its cancellation
        # reaches the stuck EOS await, which swallows it, so on Python 3.12+
        # wait_for returns normally after the full timeout and hides the hang.
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.wait_for(
            em_player.play("office", "http://radio/new"), 2.0)
        took = loop.time() - t0
        state, url = s.state, s.url
        await asyncio.wait_for(em_player.stop("office"), 2.0)
        return took, state, url, s

    try:
        took, state, url, s = asyncio.run(main())
    finally:
        em_player.EOS_WAIT_S = 2.0
    assert took < 0.5, f"play() waited {took:.2f}s on a dead link"
    assert (state, url) == (PLAYING, "http://radio/new")
    assert s.state == IDLE


def test_a_live_link_still_gets_the_eos_before_the_next_stream():
    # The bound must not reorder anything on a healthy link: the old stream's
    # EOS (which disarms the device's discard-until-EOS) precedes the new
    # stream's first frame.
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s
        await s.play("http://radio/old")
        await asyncio.sleep(0.05)
        await em_player.play("office", "http://radio/new")
        await asyncio.sleep(0.05)
        await em_player.stop("office")
        return device
    device = asyncio.run(main())
    types = [f[0] for f in device.data_frames]
    first_eos = types.index(0x03)
    assert types[:first_eos] == [0x02, 0x02]
    assert types[first_eos + 1] == 0x02
