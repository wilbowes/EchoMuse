import asyncio

import em_player
from em_player import MediaSession, SPEAKER_BYTES, PLAYING, PAUSED, IDLE


class FakeDevice:
    def __init__(self, device_id="office"):
        self.device_id = device_id
        self.eq_bands = [0.0] * 8
        self.eq_loudness = False
        self.data_frames: list[bytes] = []
        self.control_msgs: list[dict] = []

    async def send_data(self, data: bytes):
        self.data_frames.append(data)

    async def send_control(self, msg: dict):
        self.control_msgs.append(msg)


class FakeProc:
    """Stands in for the ffmpeg subprocess: N periods of PCM, then EOF
    (or never-ending if endless=True, for pause-mid-play tests)."""

    def __init__(self, periods: int, endless: bool = False):
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.killed = False
        for i in range(periods):
            self.stdout.feed_data(bytes([i % 251] * SPEAKER_BYTES))
        if not endless:
            self.stdout.feed_eof()

    def kill(self):
        self.killed = True
        self.returncode = -9


class StubSession(MediaSession):
    """MediaSession with the decoder stubbed out; records spawn calls."""

    def __init__(self, device_id, periods=3, endless=False):
        super().__init__(device_id)
        self._periods = periods
        self._endless = endless
        self.spawns: list[float] = []   # position_s per spawn
        self.procs: list[FakeProc] = []

    async def _spawn_decoder(self, url, position_s):
        self.spawns.append(position_s)
        proc = FakeProc(self._periods, self._endless)
        self.procs.append(proc)
        return proc


def setup_function(_fn):
    # Fresh module state per test; DRAIN_FUDGE_S=0 keeps natural-end
    # tests from sleeping out the device prime allowance.
    em_player._sessions.clear()
    em_player._notify_state = None
    em_player.DRAIN_FUDGE_S = 0.0


def _wire(device):
    em_player.init(
        get_device=lambda did: device if did == device.device_id else None,
        notify_state=None,
    )
    em_player._notify_state = None


def test_play_streams_frames_then_eos():
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=3)
        await s.play("http://radio/stream")
        await asyncio.wait_for(s._task, 5)
        return device, s
    device, s = asyncio.run(main())
    assert s.state == IDLE
    types = [f[0] for f in device.data_frames]
    assert types == [0x02, 0x02, 0x02, 0x03]
    # Flat EQ: payload passes through untouched
    assert device.data_frames[0][1:] == bytes([0] * SPEAKER_BYTES)


def test_pause_flushes_then_eos_and_bookmarks():
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)   # let the 2 available periods go out
        await s.pause()
        return device, s
    device, s = asyncio.run(main())
    assert s.state == PAUSED
    assert {"type": "speaker_flush"} in device.control_msgs
    # EOS goes out on teardown so the flush discard disarms
    assert device.data_frames[-1][0] == 0x03
    assert s._pos >= 0.0
    assert s.procs[0].killed


def test_resume_restarts_decoder_at_bookmark():
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await s.pause()
        s._pos = 42.0   # pretend we were deep into the track
        await s.resume()
        assert s.state == PLAYING
        await asyncio.sleep(0.05)   # let the feed task reach the spawn
        await s.stop()
        return s
    s = asyncio.run(main())
    assert s.spawns == [0.0, 42.0]


def test_stop_clears_session():
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await s.stop()
        return device, s
    device, s = asyncio.run(main())
    assert s.state == IDLE
    assert s.url is None and s._pos == 0.0


def test_interrupt_resume_cycle_only_touches_playing_sessions():
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        # Nothing playing: interrupt/resume are no-ops
        await em_player.interrupt("office")
        assert s.state == IDLE and not s.interrupted

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await em_player.interrupt("office")
        assert s.state == PAUSED and s.interrupted

        await em_player.resume_interrupted("office")
        assert s.state == PLAYING and not s.interrupted
        await s.stop()

        # User-paused (not interrupted) sessions must NOT auto-resume
        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await s.pause()
        await em_player.resume_interrupted("office")
        assert s.state == PAUSED
        await s.stop()
    asyncio.run(main())


def test_device_gone_abandons_without_wire_traffic():
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s
        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        n_control = len(device.control_msgs)
        em_player.device_gone("office")
        await asyncio.sleep(0.01)
        return device, s, n_control
    device, s, n_control = asyncio.run(main())
    assert s.state == IDLE
    assert len(device.control_msgs) == n_control  # no flush sent
    assert "office" not in em_player._sessions


def test_module_state_helpers():
    assert em_player.state("nope") == IDLE
    assert not em_player.is_playing("nope")
