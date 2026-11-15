"""
An Echo's links come and go: power cuts, WiFi drops, a controller restart.
What the person sees afterwards is an Echo that still answers.
"""

import contextlib

from harness import wait_for
from test_voice_turn import SPEECH, _reply, _turn_row, room


def test_a_replacement_data_connection_closes_the_old_one_and_answers_over_the_new(scenario):
    """#751: after a power cut mid-stream the Echo redials /data before the
    controller has noticed the old connection is dead. Decided (2026-10): the
    replacement closes the old socket straight away, rather than leaving it
    to the ping timeout (WS_PING_TIMEOUT_S, 30 s)."""
    async def body(ctl):
        async with room(ctl) as (echo, ha, tts):
            old = await echo.redial_data()
            try:
                await wait_for(lambda: old.close_code is not None, 3,
                               "the controller to close the replaced data connection")
            finally:
                with contextlib.suppress(Exception):
                    await old.close()

            reply_pcm, url = _reply(tts)
            ha.pipeline = lambda h: h.answer("what time is it", "It is noon.", url)
            await echo.wake(SPEECH)
            await wait_for(lambda: echo.voice_eos >= 1, 20, "the reply over the new connection")
            assert bytes(echo.voice)[:len(reply_pcm)] == reply_pcm
            assert (await _turn_row(ctl, echo, 1))["outcome"] == "ok"
    scenario(body)
