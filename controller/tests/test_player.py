import asyncio

import em_player
from em_player import MediaSession, SPEAKER_BYTES, PLAYING, PAUSED, IDLE


class FakeDevice:
    def __init__(self, device_id="office"):
        self.device_id = device_id
        self.eq_bands = [0.0] * 8
        self.eq_loudness = False
        self.bass_guard_enabled = True
        self.bass_guard_db = -30.0
        self.limiter_enabled = True
        self.limiter_threshold = -1.0
        self.limiter_release = 150.0
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

    async def wait(self):
        # Mirrors asyncio.subprocess.Process.wait so the double stays
        # faithful to the API the code actually calls.
        return self.returncode


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
        assert s.state == IDLE and not s.resume_after

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await em_player.interrupt("office")
        assert s.state == PAUSED and s.resume_after

        await em_player.resume_interrupted("office")
        assert s.state == PLAYING and not s.resume_after
        await s.stop()

        # User-paused (not turn-paused) sessions must NOT auto-resume
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


def test_unseekable_resume_rejoins_live_edge_instead_of_going_silent():
    """
    Regression for 2026-07-25: a voice turn over a Music Assistant flow
    stream paused at 173.6s, resumed with `-ss 173.6`, and the device stayed
    silent while HA still showed playing.

    ffmpeg's -ss is an INPUT seek. On a seekable file it is a fast demuxer
    jump; on a continuous live stream ffmpeg decodes and DISCARDS input until
    it reaches the timestamp, so the bookmark becomes a wall-clock wait. The
    feed must notice that no audio arrived and rejoin the live edge.
    """
    device = FakeDevice("lounge")
    _wire(device)
    em_player.SEEK_STALL_S = 0.05   # keep the test fast

    class UnseekableSession(StubSession):
        async def _spawn_decoder(self, url, position_s):
            self.spawns.append(position_s)
            # A seek this stream cannot honour: never emits anything.
            # Position 0 (the live edge) plays normally.
            proc = FakeProc(3, endless=(position_s > 0.5))
            if position_s > 0.5:
                proc.stdout = asyncio.StreamReader()   # silent forever
            self.procs.append(proc)
            return proc

    s = UnseekableSession("lounge", periods=3)
    s.url = "http://ma/flow/endless.flac"
    s._pos = 173.6
    s.state = PAUSED

    asyncio.run(_drive(s))

    assert s.spawns[0] == 173.6, "should try the bookmark first"
    assert 0.0 in s.spawns[1:], "did not fall back to the live edge"
    assert s.procs[0].killed, "stalled decoder was left running"
    assert device.data_frames, "device received no audio at all"


async def _drive(session):
    await session.resume()
    if session._task is not None:
        await asyncio.wait_for(session._task, timeout=5)


def test_pausing_during_a_barge_in_stays_paused():
    """
    Issue #53: "I can't actually get the music to stop and stay stopped via
    voice alone."

    The reported sequence, exactly. Music playing, wake word interrupts it,
    the user asks to pause — and before the fix all three of these went
    wrong at once: MediaSession.pause() returns early unless PLAYING so the
    command was DISCARDED, the user was told the music was already paused,
    and resume_interrupted then started it again at the end of the turn.

    The user's pause must win over our auto-resume. It is their command; ours
    is bookkeeping.
    """
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)

        # Wake word during music: we pause it for the turn.
        await em_player.interrupt("office")
        assert s.state == PAUSED and s.resume_after

        # "pause the music" — arrives through the module-level entry point,
        # which is what HA / Music Assistant drive.
        await em_player.pause("office")
        assert s.pending == ("pause", None), (
            "a user pause during a turn must be recorded as their intent"
        )

        # Turn ends. The music must STAY paused.
        await em_player.resume_interrupted("office")
        assert s.state == PAUSED, "the turn's end contradicted the user's pause"
        await s.stop()
    asyncio.run(main())


def test_barge_in_does_not_tell_home_assistant_the_music_is_paused():
    """
    Issue #62, and the reason #53's fix did not settle it.

    #53 assumed the user's pause REACHES em_player.pause(). It does not. Our
    interrupt-pause pushed PAUSED to the media_player entity, so Home
    Assistant answered a spoken "pause the music" with "it's already paused"
    and never sent the command — no intent recorded, and the auto-resume then
    put the music back. Reported as: "I get a response telling me that the
    player is already paused, and then after the response the music un-pauses
    and continues playing."

    The entity was never wrong about our state machine; it was wrong about
    what the user could see, and HA acts on the latter.
    """
    async def main():
        device = FakeDevice()
        pushed = _recording_wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        pushed.clear()

        await em_player.interrupt("office")
        assert s.state == PAUSED, "the wire must still be paused for the turn"
        assert PAUSED not in pushed, (
            "HA was told the music is paused; it will refuse to send the "
            "user's pause command and the turn will resume over the top"
        )
        assert em_player.reported_state("office") == PLAYING

        # Everything HA asks mid-turn — a volume report, turn end — must agree.
        await em_player.resume_interrupted("office")
        await asyncio.sleep(0.05)
        assert s.state == PLAYING, "the ordinary barge-in must still resume"
        await s.stop()
    asyncio.run(main())


def test_a_real_pause_during_a_turn_is_still_reported_to_home_assistant():
    """
    The other half: once the user HAS asked, the entity must say paused
    without waiting for the turn to end — otherwise the fix above just moves
    the staleness rather than removing it.
    """
    async def main():
        device = FakeDevice()
        pushed = _recording_wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await em_player.interrupt("office")
        pushed.clear()

        await em_player.pause("office")
        assert PAUSED in pushed, "HA was not told the user's pause took effect"
        assert em_player.reported_state("office") == PAUSED

        await em_player.resume_interrupted("office")
        assert s.state == PAUSED, "the turn's end contradicted the user's pause"
        await s.stop()
    asyncio.run(main())


def test_stop_during_a_barge_in_stays_stopped():
    """
    The behaviour pause is being made consistent with. Worth pinning: it is
    the reason the bug was pause-only, and a later refactor could easily
    break it while "fixing" pause.
    """
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await em_player.interrupt("office")
        await em_player.stop("office")
        assert s.pending == ("stop", None)

        await em_player.resume_interrupted("office")
        assert s.state == IDLE
    asyncio.run(main())


def test_an_ordinary_barge_in_still_resumes():
    """
    The fix must not cost the normal case: barge in, ask something unrelated,
    music comes back on its own.
    """
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await em_player.interrupt("office")
        await em_player.resume_interrupted("office")
        assert s.state == PLAYING and not s.resume_after
        await s.stop()
    asyncio.run(main())


def test_play_during_a_turn_waits_for_the_speaker():
    """
    The common collision, and the reason this is an ownership model rather
    than a patch to resume(): "play some jazz" runs the intent BEFORE Home
    Assistant generates the spoken reply, so play_media can arrive while the
    TTS is still coming — putting music on the same 0x02 plane as the
    response.

    Nothing was playing when the turn began, which is exactly the case the
    old interrupt() ignored: it only paused what was ALREADY playing and did
    nothing to stop something starting.
    """
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await em_player.interrupt("office")          # turn starts, nothing playing
        await em_player.play("office", "http://radio/jazz")

        assert s.state == IDLE, "music must not start while the turn owns the speaker"
        assert not s.spawns, "no decoder should be spawned mid-turn"
        assert s.pending == ("play", "http://radio/jazz")

        await em_player.resume_interrupted("office")  # turn ends
        await asyncio.sleep(0.05)
        assert s.state == PLAYING
        assert s.url == "http://radio/jazz"
        await s.stop()
    asyncio.run(main())


def test_resume_during_a_turn_waits_for_the_speaker():
    """The narrow case that started this: resume() used to start the feed
    immediately, mid-turn, straight into the TTS."""
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await s.pause()                               # user paused it earlier
        await em_player.interrupt("office")           # then a turn starts

        await em_player.resume("office")
        assert s.state == PAUSED, "resume must not start the feed mid-turn"

        await em_player.resume_interrupted("office")
        await asyncio.sleep(0.05)
        assert s.state == PLAYING
        await s.stop()
    asyncio.run(main())


def test_the_last_command_in_a_turn_wins():
    """
    "Play jazz... actually, pause" — the last instruction is what was meant.
    Anything else would replay a command the user changed their mind about.
    """
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await em_player.interrupt("office")
        await em_player.play("office", "http://radio/jazz")
        await em_player.pause("office")
        assert s.pending == ("pause", None)

        await em_player.resume_interrupted("office")
        await asyncio.sleep(0.05)
        assert s.state == IDLE, "the abandoned play must not start"
        assert not s.spawns
    asyncio.run(main())


def test_a_user_command_overrides_our_auto_resume():
    """
    Ownership and resume_after are separate facts: we paused the music, but
    the user then said something about it. Theirs is an instruction, ours is
    bookkeeping.
    """
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await em_player.interrupt("office")
        assert s.resume_after

        await em_player.stop("office")
        await em_player.resume_interrupted("office")
        assert s.state == IDLE, "the user's stop lost to our auto-resume"
        assert s.url is None
    asyncio.run(main())


def test_home_assistant_is_told_the_intent_not_left_stale():
    """
    Deferring must not leave the media_player entity showing the wrong thing
    for the length of a turn — #53's other half is already a complaint about
    that entity being wrong.
    """
    async def main():
        device = FakeDevice()
        _wire(device)
        pushed: list[tuple[str, str]] = []

        async def notify(did, state):
            pushed.append((did, state))
        em_player._notify_state = notify

        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await em_player.interrupt("office")
        await em_player.play("office", "http://radio/jazz")
        assert ("office", PLAYING) in pushed, \
            "HA was not told the deferred play would happen"
        await em_player.resume_interrupted("office")
        await asyncio.sleep(0.05)
        await s.stop()
    asyncio.run(main())


def _recording_wire(device):
    """Wire em_player with a notify_state that records what HA is told."""
    pushed: list[str] = []

    async def notify(did, state):
        pushed.append(state)
    em_player.init(get_device=lambda did: device if did == device.device_id else None,
                   notify_state=notify)
    em_player._notify_state = notify
    return pushed


def test_resume_tells_home_assistant_it_is_playing_again():
    """
    Issue #53: "the media player reports that it is idle even though the music
    continues to play on the echo."

    The feed announces PLAYING exactly ONCE, when the decoder starts producing
    audio — so whatever is sent to HA after that is the last word. The turn-end
    message was a hardcoded IDLE, which silently became that last word.

    This pins the announcement itself: without it there is nothing for the
    turn-end fix to be correct about.
    """
    async def main():
        device = FakeDevice()
        pushed = _recording_wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        await s.pause()
        pushed.clear()

        await s.resume()
        await asyncio.sleep(0.05)
        assert PLAYING in pushed, "HA was never told playback resumed"
        await s.stop()
    asyncio.run(main())


def test_play_tells_home_assistant_it_is_playing():
    """
    play() begins with stop(), which pushes IDLE. The PLAYING that follows
    comes from the feed, not from play() itself — worth pinning, because the
    obvious "fix" of pushing in play()/resume() duplicates it.
    """
    async def main():
        device = FakeDevice()
        pushed = _recording_wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await s.play("http://radio/stream")
        await asyncio.sleep(0.05)
        assert pushed[-1] == PLAYING, f"last state HA saw was {pushed[-1]!r}"
        await s.stop()
    asyncio.run(main())


def test_playback_state_is_pushed_once_per_start_not_per_chunk():
    """
    It rides the start of the feed, not the send loop — a push per audio period
    would be a message storm on the plane carrying the audio.

    Once-only is also exactly why the turn-end IDLE was able to win: there is
    no later push to correct it.
    """
    async def main():
        device = FakeDevice()
        pushed = _recording_wire(device)
        s = StubSession("office", periods=6)
        em_player._sessions["office"] = s

        await s.play("http://radio/stream")
        await asyncio.sleep(0.2)
        assert pushed.count(PLAYING) == 1, f"pushed PLAYING {pushed.count(PLAYING)} times"
    asyncio.run(main())


def test_a_starved_decoder_is_reported_as_a_source_stall(caplog):
    """
    The measurement that separates the two causes of an audible dropout.

    The device reports a gap between frames ARRIVING, which looks identical
    whether the controller had nothing to send (source starving — a Music
    Assistant flow stalling upstream) or the link swallowed it. send_ms cannot
    settle it either: a socket write completes near-instantly however slow the
    wire is.

    So a device-side gap WITH one of these logged at the same moment is
    upstream; a device-side gap WITHOUT one is the link. Observed 2026-08-01:
    minDepth=0 underruns=1 on Lounge with RTT excursions to 1637ms, and no way
    to tell which.
    """
    import logging

    async def main():
        device = FakeDevice()
        _wire(device)

        class StallingSession(StubSession):
            async def _spawn_decoder(self, url, position_s):
                proc = await super()._spawn_decoder(url, position_s)
                real_read = proc.stdout.readexactly
                calls = {"n": 0}

                async def slow(n):
                    calls["n"] += 1
                    if calls["n"] == 2:          # starve once, mid-stream
                        await asyncio.sleep(0.6)
                    return await real_read(n)
                proc.stdout.readexactly = slow
                return proc

        s = StallingSession("office", periods=4)
        em_player._sessions["office"] = s
        em_player.SOURCE_STALL_MS = 200.0        # keep the test quick

        with caplog.at_level(logging.WARNING, logger="player"):
            await s.play("http://radio/stream")
            await asyncio.sleep(1.2)

        assert any("SOURCE stall" in r.message for r in caplog.records), \
            "a starved decoder must be distinguishable from a link stall"
    asyncio.run(main())
    em_player.SOURCE_STALL_MS = 500.0


def test_pacing_sleep_is_not_mistaken_for_a_source_stall():
    """
    The feed deliberately sleeps to hold the lead at LEAD_S. Timing the whole
    loop iteration instead of just the read would report every healthy stream
    as permanently stalled — which is the failure mode that makes an
    instrument worse than none, because it is wrong in the reassuring
    direction of looking like a real problem everywhere.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_player.py").read_text()
    fn = src[src.index("    async def _feed(self)"):]
    read = fn[fn.index("_t0 = loop.time()"):fn.index("_read_ms = ")]
    assert "asyncio.sleep" not in read, \
        "the pacing sleep must sit outside the timed read"


def test_an_unseekable_stream_is_only_discovered_once():
    """
    Rediscovering unseekability costs SEEK_STALL_S of SILENCE every time.

    A Music Assistant flow is not seekable, so the first resume spends 5s
    waiting for audio that never comes before rejoining the live edge. Barge
    in three times during a song and that is 15s of dead air for an answer we
    already had after the first one.
    """
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2, endless=True)
        em_player._sessions["office"] = s

        await s.play("http://ma/flow/abc")
        assert s._seekable, "a fresh URL must start out assumed seekable"

        # First resume discovers it the expensive way.
        s._seekable = False
        s._pos = 12.6
        await s.pause()
        await s.resume()
        await asyncio.sleep(0.05)
        assert s._pos == 0.0, "an unseekable resume must go to the live edge"

        # A new URL is a new question.
        await s.play("http://ma/track/def")
        assert s._seekable, "seekability must not leak across URLs"
        await s.stop()
    asyncio.run(main())


class MixingDevice(FakeDevice):
    """A device announcing the audio_mix capability."""
    audio_mix_capable = True


def test_music_rides_its_own_plane_on_a_mixing_device():
    """
    0x04/0x05, so the device can hold it separately from voice and mix the
    two. On 0x02 it would share the voice plane and ducking is impossible.
    """
    async def main():
        device = MixingDevice()
        _wire(device)
        s = StubSession("office", periods=3)
        await s.play("http://radio/stream")
        await asyncio.wait_for(s._task, 5)
        return device
    device = asyncio.run(main())
    assert [f[0] for f in device.data_frames] == [0x04, 0x04, 0x04, 0x05]


def test_music_stays_on_the_voice_plane_for_older_firmware():
    """
    Degrade to the old behaviour, never to silence: firmware without the
    capability would simply never play a 0x04 frame.
    """
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2)
        await s.play("http://radio/stream")
        await asyncio.wait_for(s._task, 5)
        return device
    device = asyncio.run(main())
    assert [f[0] for f in device.data_frames] == [0x02, 0x02, 0x03]


def test_a_turn_ducks_instead_of_pausing_when_the_device_can_mix():
    """
    The whole point. Pausing costs a seek, and a Music Assistant flow stream
    cannot seek — so a 28s turn cost 28s of the song. Ducking keeps the music
    playing under the response and loses nothing.
    """
    async def main():
        device = MixingDevice()
        _wire(device)
        s = StubSession("office", periods=3, endless=True)
        em_player._sessions["office"] = s
        await s.play("http://radio/stream")
        await asyncio.sleep(0)
        await em_player.interrupt("office")
        state_during = s.state
        await em_player.resume_interrupted("office")
        await s.stop()
        return device, s, state_during
    device, s, state_during = asyncio.run(main())

    assert state_during == PLAYING, "the music must keep playing under the turn"
    ducks = [m for m in device.control_msgs if m.get("type") == "duck"]
    assert [m["on"] for m in ducks] == [True, False]
    assert not any(m.get("type") == "speaker_flush" for m in device.control_msgs), \
        "ducking must not flush — that discards the buffer that makes it instant"
    assert s.spawns == [0.0], "no reseek: the stream was never interrupted"


def test_a_turn_still_pauses_on_firmware_that_cannot_mix():
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=3, endless=True)
        em_player._sessions["office"] = s
        await s.play("http://radio/stream")
        await asyncio.sleep(0)
        await em_player.interrupt("office")
        paused = s.state
        await em_player.resume_interrupted("office")
        await s.stop()
        return device, paused
    device, paused = asyncio.run(main())
    assert paused == PAUSED
    assert not any(m.get("type") == "duck" for m in device.control_msgs), \
        "a device that cannot mix must never be told to duck"


def test_pausing_during_a_ducked_turn_actually_pauses():
    """
    On the pausing path interrupt() had already paused, so the deferred
    "pause" needed no wire action. When we duck instead, nothing was ever
    paused — so the user's pause has to actually happen at turn end, or it is
    silently dropped and the music plays on.
    """
    async def main():
        device = MixingDevice()
        _wire(device)
        s = StubSession("office", periods=3, endless=True)
        em_player._sessions["office"] = s
        await s.play("http://radio/stream")
        await asyncio.sleep(0)
        await em_player.interrupt("office")
        await em_player.pause("office")          # user speaks: "pause the music"
        await em_player.resume_interrupted("office")
        return device, s
    device, s = asyncio.run(main())
    assert s.state == PAUSED, "the user's pause must survive a ducked turn"


def test_stopping_music_flushes_the_music_plane_not_the_voice_one():
    """
    speaker_flush would cut the response and leave the music playing, which
    is exactly backwards.
    """
    async def main():
        device = MixingDevice()
        _wire(device)
        s = StubSession("office", periods=3, endless=True)
        await s.play("http://radio/stream")
        await asyncio.sleep(0)
        await s.stop()
        return device
    device = asyncio.run(main())
    kinds = [m.get("type") for m in device.control_msgs]
    assert "music_flush" in kinds
    assert "speaker_flush" not in kinds


def test_the_music_feed_yields_the_wire_during_a_turn():
    """
    Music and TTS share one WebSocket, so a 4s music lead puts ~380KB ahead
    of the response in the socket. Measured on the first ducked turn: the
    response waited 2087ms to start and took a 3549ms gap mid-stream. The
    feed drops its lead for the turn so the response gets the wire.
    """
    async def main():
        device = MixingDevice()
        _wire(device)
        s = StubSession("office", periods=3, endless=True)
        em_player._sessions["office"] = s
        await s.play("http://radio/stream")
        await asyncio.sleep(0)
        before = s.lead_s
        await em_player.interrupt("office")
        during = s.lead_s
        await em_player.resume_interrupted("office")
        after = s.lead_s
        await s.stop()
        return before, during, after
    before, during, after = asyncio.run(main())
    assert before == em_player.LEAD_S
    assert during == em_player.TURN_LEAD_S, "the feed must yield during a turn"
    assert during < before
    assert after == em_player.LEAD_S, "and take the wire back afterwards"


def test_the_reduced_lead_still_protects_the_music():
    """
    Not zero: at zero the music would be sent at the instant it is needed,
    with no margin against a stall DURING the turn — trading one dropout for
    another.
    """
    assert em_player.TURN_LEAD_S > 0


def test_a_pausing_device_does_not_change_its_lead():
    """
    On firmware that cannot mix, music is paused for the turn, so there is
    nothing sharing the wire and nothing to yield.
    """
    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=3, endless=True)
        em_player._sessions["office"] = s
        await s.play("http://radio/stream")
        await asyncio.sleep(0)
        await em_player.interrupt("office")
        during = s.lead_s
        await em_player.resume_interrupted("office")
        await s.stop()
        return during
    assert asyncio.run(main()) == em_player.LEAD_S


# ── #150: a source that stops producing ends the stream and reports idle ────


class DeadSourceProc(FakeProc):
    """Delivers a few periods, then hangs forever: no EOF (the decoder is
    still alive, parked on a dead upstream), no audio. The shape of a
    starving Music Assistant flow."""

    def __init__(self, periods):
        super().__init__(periods, endless=True)
        real_readexactly = self.stdout.readexactly

        served = {"n": 0}
        total = periods

        async def hang_eventually(n):
            if served["n"] >= total:
                await asyncio.sleep(3600)     # never returns
            served["n"] += 1
            return await real_readexactly(n)

        self.stdout.readexactly = hang_eventually


def test_a_dead_source_ends_the_stream_and_reports_idle(caplog, monkeypatch):
    """
    #150: the read had no deadline, so a source that stopped producing left
    the feed parked forever - player log silent, HA showing `playing`
    indefinitely, decoder held. A source that produces nothing for
    SOURCE_DEAD_S must end the stream, send EOS so the device settles, and
    let HA report idle.
    """
    import logging
    monkeypatch.setattr(em_player, "SOURCE_DEAD_S", 0.2)

    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=2)
        s._spawn_decoder = lambda url, pos: _spawn(url, pos, s)

        async def _spawn(url, pos, s):
            proc = DeadSourceProc(2)
            s.procs.append(proc)
            return proc

        with caplog.at_level(logging.ERROR, logger="player"):
            await s.play("http://ma/flow/stream")
            await asyncio.wait_for(s._task, 10)
        return device, s

    device, s = asyncio.run(main())
    assert s.state == IDLE, "a dead source must not leave the session playing"
    assert device.data_frames[-1][0] == 0x03, \
        "EOS must reach the device so its ring and buffer settle"
    text = "\n".join(r.message for r in caplog.records)
    assert "produced nothing" in text, \
        "the log must say the source died, not that the media finished"


def test_a_slow_but_recovering_source_is_not_killed(monkeypatch):
    """
    The deadline exists for DEAD sources. One that stalls past the stall
    warning but returns inside the deadline keeps streaming.
    """
    monkeypatch.setattr(em_player, "SOURCE_DEAD_S", 0.5)

    async def main():
        device = FakeDevice()
        _wire(device)
        s = StubSession("office", periods=3)

        class SlowThenFineProc(FakeProc):
            def __init__(self):
                super().__init__(3, endless=False)
                real = self.stdout.readexactly
                calls = {"n": 0}

                async def slow(n):
                    calls["n"] += 1
                    if calls["n"] == 2:
                        await asyncio.sleep(0.2)   # over SOURCE_STALL_MS,
                    return await real(n)           # under SOURCE_DEAD_S
                self.stdout.readexactly = slow

        async def spawn(url, pos):
            p = SlowThenFineProc()
            s.procs.append(p)
            return p
        s._spawn_decoder = spawn

        await s.play("http://ma/flow/stream")
        await asyncio.wait_for(s._task, 10)
        return device, s

    device, s = asyncio.run(main())
    assert s.state == IDLE
    types = [f[0] for f in device.data_frames]
    assert types.count(0x02) == 3 and types[-1] == 0x03, \
        "every period must have made it out before EOS"
