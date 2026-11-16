"""
Timers ring on the Echo that set them, and are stopped by voice from anywhere
in earshot.
"""

import asyncio
import contextlib
import shutil

from aioesphomeapi import VoiceAssistantTimerEventType as T

from harness import FakeEcho, FakeHA, wait_for
from test_voice_turn import SPEECH, _connect


@contextlib.asynccontextmanager
async def two_echoes(ctl):
    """Two Echoes in one room, each with HA linked to its satellite."""
    pairs = []
    try:
        for serial in ("G0K0SCEN00A1", "G0K0SCEN00B2"):
            echo = FakeEcho(ctl, device_id=serial)
            await echo.connect()
            port = await wait_for(lambda: ctl.esphome_port(serial), 10,
                                  f"{serial}'s satellite port")
            ha = FakeHA(port)
            await _connect(ha)
            pairs.append((echo, ha))
        yield pairs
    finally:
        for echo, ha in pairs:
            await ha.disconnect()
            await echo.close()


def _won_by(ctl, serial: str) -> list[str]:
    return [line.split("won by ", 1)[1] for line in ctl.log().splitlines()
            if f"[{serial}] arbitration" in line and "won by " in line]


def test_a_wake_word_over_a_ringing_timer_goes_to_the_echo_ringing(scenario):
    """#862: …0C95 was ringing; the wake word went to …15LE, which heard it
    first, and the wrong Echo lit. Here the neighbour hears it first AND
    louder, so neither time nor level would give it to the ringer."""
    assert shutil.which("ffmpeg"), "the alarm sound is decoded with ffmpeg"

    async def body(ctl):
        async with two_echoes(ctl) as [(near, _), (ringer, ringer_ha)]:
            ringer_ha.client.send_voice_assistant_timer_event(
                T.VOICE_ASSISTANT_TIMER_FINISHED, "t1", "pasta", 60, 0, False)
            await wait_for(lambda: ringer.voice, 10, "the alarm to play on the ringing Echo")

            # One utterance, heard by both: the neighbour 60 ms sooner, 20 dB louder.
            near_wake = asyncio.create_task(
                near.wake(SPEECH, level=-40.0, peak=-30.0, barge=False))
            await asyncio.sleep(0.06)
            await ringer.wake(SPEECH, level=-60.0, peak=-50.0, barge=False)
            await near_wake

            await wait_for(lambda: _won_by(ctl, near.device_id) and _won_by(ctl, ringer.device_id),
                           5, "both claims to be decided")
            assert _won_by(ctl, ringer.device_id)[0] == f"{ringer.device_id} (ringing)"
            assert _won_by(ctl, near.device_id)[0].startswith(ringer.device_id)
    scenario(body)
