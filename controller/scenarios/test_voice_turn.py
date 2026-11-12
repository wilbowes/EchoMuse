"""
Voice turns as a person meets them: say the wake word, ask, hear the answer;
and the ways that goes wrong that people have actually hit.

Each test runs the real controller (harness.Controller), an Echo speaking the
wire contract (FakeEcho) and Home Assistant as aioesphomeapi (FakeHA).
"""

import asyncio
import contextlib

import harness
from harness import FakeEcho, FakeHA, TtsServer, wait_for

SPEECH = harness.tone(1.2, harness.MIC_RATE, hz=200)


@contextlib.asynccontextmanager
async def room(ctl, *, with_ha: bool = True):
    """An Echo connected to the controller, and HA linked to its satellite."""
    tts = TtsServer()
    await tts.start()
    echo = FakeEcho(ctl)
    await echo.connect()
    assert "listen_session" in echo.features, "private listening not negotiated"
    port = await wait_for(lambda: ctl.esphome_port(echo.device_id), 10,
                          "the Echo's satellite port")
    ha = FakeHA(port)
    if with_ha:
        await _connect(ha)
    try:
        yield echo, ha, tts
    finally:
        await ha.disconnect()
        await echo.close()
        await tts.stop()


async def _connect(ha: FakeHA) -> None:
    """HA dials the satellite port; the server may still be coming up."""
    last = None
    for _ in range(50):
        try:
            await ha.connect()
            return
        except Exception as e:
            last = e
            await asyncio.sleep(0.2)
    raise AssertionError(f"Home Assistant could not connect: {last}")


def _reply(tts, name="reply.wav", seconds=1.5):
    pcm = harness.tone(seconds, 48000, hz=330)
    return pcm, tts.add(name, harness.wav_bytes(pcm, 48000))


async def _turn_row(ctl, echo, n: int) -> dict:
    turns = await wait_for(lambda: len(ctl.turns(echo.device_id)) >= n and
                           ctl.turns(echo.device_id), 10, f"turn row {n}")
    return turns[n - 1]


def test_ask_a_question_and_hear_the_answer(scenario):
    async def body(ctl):
        async with room(ctl) as (echo, ha, tts):
            reply_pcm, url = _reply(tts)
            ha.pipeline = lambda h: h.answer("what time is it", "It is noon.", url)

            sid = await echo.wake(SPEECH)

            # The wake was taken, and what was said reached Home Assistant.
            await wait_for(lambda: any(m.get("session") == sid for m in echo.sent("listen_ack")),
                           5, "the controller to acknowledge the wake")
            assert ha.starts, "the satellite never asked HA for a run"
            assert len(ha.audio) > 0

            # The answer played on the Echo, whole, and ended.
            await wait_for(lambda: echo.voice_eos >= 1, 20, "the reply to finish")
            assert tts.fetched == ["reply.wav"]
            # Every sample of the reply, unaltered (an output_chain Echo gets
            # the chain as passthrough and runs it itself), then at most one
            # period of silence padding out the last write.
            got = bytes(echo.voice)
            assert got[:len(reply_pcm)] == reply_pcm, "the reply arrived altered or cut short"
            pad = got[len(reply_pcm):]
            assert pad == bytes(len(pad)) and len(pad) < 4096, (
                f"{len(pad)} bytes after the reply, not just its last period's padding")

            # The session was closed: the Echo is not left streaming.
            assert echo.closed(sid), (
                "the session was never closed: the Echo would stream until maxOpen")

            # The turn is on record as answered, with what was heard.
            row = await _turn_row(ctl, echo, 1)
            assert row["outcome"] == "ok", row
            assert row["stt_text"] == "what time is it"

    scenario(body)


def test_two_questions_in_a_row_are_both_answered(scenario):
    """The protocol has no run id, so a late event from the first run can land
    on the second. Both must be answered, in full."""
    async def body(ctl):
        async with room(ctl) as (echo, ha, tts):
            first, url1 = _reply(tts, "one.wav", 1.0)
            second, url2 = _reply(tts, "two.wav", 0.7)

            ha.pipeline = lambda h: h.answer("turn on the lights", "Done.", url1)
            await echo.wake(SPEECH)
            await wait_for(lambda: echo.voice_eos >= 1, 20, "the first reply")
            ha.audio.clear()

            ha.pipeline = lambda h: h.answer("and the fan", "Done again.", url2)
            await echo.wake(SPEECH)
            await wait_for(lambda: echo.voice_eos >= 2, 20, "the second reply")

            assert tts.fetched == ["one.wav", "two.wav"]
            row1, row2 = await _turn_row(ctl, echo, 1), await _turn_row(ctl, echo, 2)
            assert (row1["outcome"], row2["outcome"]) == ("ok", "ok"), (row1, row2)
            assert row2["stt_text"] == "and the fan"

    scenario(body)


def test_with_no_home_assistant_the_echo_is_told_and_stops_listening(scenario):
    """An Echo nobody linked in HA: the wake is recorded, the Echo is closed
    at once (not left streaming to its 30 s limit), and nothing plays."""
    async def body(ctl):
        async with room(ctl, with_ha=False) as (echo, ha, tts):
            sid = await echo.wake(SPEECH)
            await wait_for(lambda: echo.closed(sid), 5, "the session to be closed")
            assert not echo.max_open_hit
            row = await _turn_row(ctl, echo, 1)
            assert row["outcome"] == "no_ha", row
            assert echo.voice_eos == 0 and not echo.voice

    scenario(body)


def test_an_error_from_home_assistant_ends_the_turn_and_the_next_one_works(scenario):
    """HA's pipeline fails mid-run (STT down, say). The turn ends, the Echo
    stops streaming, and the next question is answered normally."""
    async def body(ctl):
        async with room(ctl) as (echo, ha, tts):
            async def broken(h):
                h.event("RUN_START")
                h.event("STT_START")
                await wait_for(lambda: len(h.audio) > 0, 5, "mic audio")
                h.event("ERROR", {"code": "stt-provider-unavailable",
                                  "message": "Speech-to-text is not available"})
                h.event("RUN_END")
            ha.pipeline = broken
            sid = await echo.wake(SPEECH)
            await wait_for(lambda: echo.closed(sid), 10, "the failed turn's session to close")
            row = await _turn_row(ctl, echo, 1)
            assert row["outcome"] != "ok", row
            assert echo.voice_eos == 0, "something played for a turn that failed"

            reply, url = _reply(tts)
            ha.audio.clear()
            ha.pipeline = lambda h: h.answer("is it working now", "Yes.", url)
            await echo.wake(SPEECH)
            await wait_for(lambda: echo.voice_eos >= 1, 20, "the next reply")
            assert (await _turn_row(ctl, echo, 2))["outcome"] == "ok"

    scenario(body)


def test_when_has_vad_never_engages_the_controller_ends_the_turn(scenario):
    """HA's VAD sometimes never engages and would run the turn to its 15 s cap
    (3.2% of wake turns, em_turnclock). The controller endpoints it itself
    shortly after the speech stops."""
    async def body(ctl):
        async with room(ctl) as (echo, ha, tts):
            reply, url = _reply(tts)

            async def deaf_vad(h):
                h.event("RUN_START")
                h.event("STT_START")
                # No STT_VAD_START, ever. HA answers once the audio ends.
                await wait_for(lambda: h.stops, 12, "the satellite to end the audio")
                h.event("STT_END", {"text": "what is the weather"})
                h.event("INTENT_START")
                h.event("INTENT_END", {"conversation_id": "c1"})
                h.event("TTS_START", {"text": "Sunny."})
                h.event("TTS_END", {"url": url})
                h.event("RUN_END")
            ha.pipeline = deaf_vad

            t0 = asyncio.get_running_loop().time()
            sid = await echo.wake(SPEECH)
            await wait_for(lambda: echo.closed(sid), 10, "the controller to endpoint the turn")
            took = asyncio.get_running_loop().time() - t0
            assert took < 6, f"took {took:.1f}s to end a turn whose speech lasted 1.2s"
            await wait_for(lambda: echo.voice_eos >= 1, 20, "the reply")

    scenario(body)


def test_a_late_run_end_from_an_abandoned_turn_does_not_kill_the_next(scenario):
    """A wake nobody speaks after ends on the controller's no-speech timeout
    while HA's run is still open. HA's RUN_END for that run can arrive after
    the next turn has started, and the protocol has no run id to tell them
    apart; unless the first run was aborted (em_runbarrier), the stale RUN_END
    ends the new turn before it is answered."""
    async def body(ctl):
        async with room(ctl) as (echo, ha, tts):
            reply, url = _reply(tts)

            async def slow_then_stale(h):
                h.event("RUN_START")
                h.event("STT_START")
                # Hold the run open until the next turn has asked for its own.
                await wait_for(lambda: len(h.starts) >= 2, 20, "the second turn",
                               step=0.005)
                h.event("RUN_END")
            ha.pipeline = slow_then_stale
            sid = await echo.wake(b"")          # the wake, then silence
            await wait_for(lambda: echo.closed(sid), 10, "the empty turn to end")
            assert (await _turn_row(ctl, echo, 1))["outcome"] == "no_speech"

            ha.audio.clear()
            async def second(h):
                # HA takes a moment to set a run up; the stale RUN_END above
                # lands in that gap, before this run's RUN_START.
                await asyncio.sleep(0.3)
                await h.answer("are you there", "I am.", url)
            ha.pipeline = second
            await echo.wake(SPEECH)
            await wait_for(lambda: echo.voice_eos >= 1, 20, "the second turn's reply")
            assert (await _turn_row(ctl, echo, 2))["outcome"] == "ok"

    scenario(body)
