"""
em_earlytts.py — when a voice turn may start speaking before HA has finished the reply

Pure (asyncio only), so it can be tested: the suite cannot import em_esphome.
See em_barge for the same reasoning.

WHY A TURN CAN START SPEAKING BEFORE TTS_END
--------------------------------------------
Home Assistant tells a satellite about the reply twice.

* `RUN_START` carries the TTS URL (`url`), before anything has been said.
* `INTENT_PROGRESS` with `tts_start_streaming == "1"` arrives when the reply's
  first text does. From then on the URL is live: HA feeds the text to its TTS
  engine as the agent writes it, and the response body is audio as each
  sentence is synthesised.

`TTS_END` is the only other place the URL appears, and HA emits it when
`recognize_intent` returns (assist_pipeline/pipeline.py: `text_to_speech()` runs
after the intent stage), which is after the agent has written the last word.
A satellite that waits for it starts speaking when the whole reply exists, and
every second of generation sits inside the 30s TTS wait in em_esphome.

ESPHome's own firmware plays from the early signal (esphome/components/
voice_assistant/voice_assistant.cpp): RUN_START stores `url`; INTENT_PROGRESS
with `tts_start_streaming == "1"` and a stored url starts the media player and
clears the stored url; TTS_END then does not start it again. This module lets
EchoMuse follow the same contract, when the `streamReply` setting is on.

Measured 2026-09-21 on an Echo Dot, HA 2026.9.3, controller 2.24.1, Wyoming
TTS (Kokoro) and a streaming conversation agent, one two-paragraph reply
spoken twice:

    controller           STT done -> first audio    audio delivered
    waits for TTS_END           5.83 s                 6,094,848 bytes
    plays on the signal         3.02 s                 6,064,128 bytes

HA's own event stream for the same reply put `tts_start_streaming` at 1.29 s
and `tts-end` at 5.59 s. Short replies gain little, because their text starts
under a second before they end, and cannot be slower. Local intents and errors
never send the signal and take the TTS_END path unchanged. The 30s wait now
has to reach the first text rather than the last.

WHY IT IS A SETTING, AND OFF BY DEFAULT
---------------------------------------
Starting early changes when speech begins, and with it what a slow backend
sounds like. The reply reaches the speaker no faster than it is produced, so
audio is spoken at the rate of the slowest stage: the model writing the text,
or the TTS engine synthesising it. The 2026-09-21 measurements above are from
a local model far faster than speech and a TTS engine several times faster than
realtime, where nothing runs dry. A model that writes more slowly than the reply
is spoken (very roughly under 4 tokens a second for prose) or a TTS engine
slower than realtime leaves the device's buffer empty between sentences. It
plays silence until more arrives and counts an underrun; the reply is not lost,
but it pauses. Waiting for TTS_END avoids that on such setups, at the cost of
the silence before the reply and the 30s limit. Which is better depends on
the backend, and the controller cannot know the backend, so the choice is the
user's and the default is the behaviour that existed before. A pure decision
function takes `enabled` for that reason, and the per-turn value is read from the
device's `streamReply` setting.

WHY AN ERROR HAS TO END THE FETCH
---------------------------------
An early fetch stays open until HA has the whole reply. When the agent fails,
HA emits ERROR and calls `ResultStream.delete()`, which drops the token from
its registry but never closes a response already in flight: the queue feeding
the TTS engine is only closed on the success path. Left alone the fetch sits
out its 60s `sock_read`, holding the speaker and the mic. `abortable_stream`
ends it as soon as the caller says HA has errored.

It also has to survive the consumer being cancelled while it waits for the next
chunk, which is exactly what a barge-in during playback does. Closing an async
generator that is in the middle of a fetch raises `aclose(): asynchronous
generator is already running`, and the fetch keeps running; so the pending
fetch is cancelled and awaited before the source is closed. tests/
test_earlytts.py pins each of these.
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, AsyncIterator, Callable, Optional

# The INTENT_PROGRESS field HA sets when the reply's text has started. ESPHome
# compares against the literal "1", and so does this.
START_FIELD = "tts_start_streaming"


def run_start_url(data: dict[str, str]) -> Optional[str]:
    """
    The TTS URL HA announced in RUN_START, or None.

    HA sends none when the run has no TTS stage. An empty string reads the same
    as absent, since a falsy url is never something to fetch.
    """
    return data.get("url") or None


def should_start(*, enabled: bool, progress: dict[str, str], announced_url: Optional[str],
                 playing_url: Optional[str], cancelled: bool) -> bool:
    """
    Whether this INTENT_PROGRESS event releases the announced URL for playback.

    `enabled` is the device's `streamReply` setting for this turn; when it is off
    the answer is always no and the turn plays from TTS_END as it always did.
    `progress` is the event's name/value data. `announced_url` is what
    `run_start_url` returned for this turn. `playing_url` is the URL the turn has
    already committed to, from an earlier signal or from TTS_END, and a second
    signal must not restart it (ESPHome clears its stored url for the same
    reason). `cancelled` is a turn that has been cut off, where starting audio
    would speak over the barge that cancelled it.
    """
    return (
        enabled
        and progress.get(START_FIELD) == "1"
        and bool(announced_url)
        and not playing_url
        and not cancelled
    )


async def abortable_stream(chunks: AsyncIterator[bytes], abort: asyncio.Event) -> AsyncGenerator[bytes, None]:
    """
    Yield from `chunks` until `abort` is set, then end the stream.

    Ending is a normal return: whatever was yielded still plays out, and the
    source generator is closed. Errors from the source propagate. If the
    consumer is cancelled while a chunk is pending, the pending fetch is
    cancelled and awaited before the source is closed (see the module notes).
    """
    it = chunks.__aiter__()
    stop = asyncio.ensure_future(abort.wait())
    nxt: Optional[asyncio.Future] = None
    try:
        while True:
            nxt = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({nxt, stop}, return_when=asyncio.FIRST_COMPLETED)
            if nxt not in done:
                return  # aborted; the finally cancels the pending fetch
            try:
                chunk = nxt.result()
            except StopAsyncIteration:
                return
            nxt = None
            yield chunk
    finally:
        stop.cancel()
        if nxt is not None:
            if not nxt.done():
                nxt.cancel()
                await asyncio.wait({nxt})
            if not nxt.cancelled():
                nxt.exception()  # mark it retrieved
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            await aclose()


async def wait_until(predicate: Callable[[], bool], timeout: float, step: float = 0.05) -> bool:
    """
    Poll `predicate` every `step` seconds for up to `timeout`; True if it held.

    For a turn that finished playing an early stream a moment before HA's
    INTENT_END, which carries `continue_conversation`. The flag is set by an
    event handler, not something awaitable, so a short poll is the whole tool.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(step)
    return True
