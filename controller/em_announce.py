"""
em_announce.py — running an HA announcement to completion.

`VoiceAssistantAnnounceFinished` is Home Assistant's completion signal, and HA
BLOCKS on it. `assist_satellite.entity.async_internal_announce` documents
`async_announce` as "should block until the announcement is done playing",
holds `_is_announcing` and the RESPONDING state for its duration, and raises
`SatelliteBusyError` if another announcement arrives meanwhile; the esphome
integration implements that by awaiting the reply through
`send_voice_assistant_announcement_await_response`.

Two rules follow, and they pull in opposite directions:

  * do not reply early — an early reply returns the service call while audio is
    still playing, drops the entity out of RESPONDING, and lets two chained
    announcements overlap on the device instead of queueing behind HA's guard;
  * always reply — a reply that never arrives parks HA for
    `_ANNOUNCEMENT_TIMEOUT_SEC` (5 minutes) holding `_is_announcing`, after
    which every announcement fails. `success=False` is strictly better than
    silence.

Split out of em_esphome so it can be tested: the controller test suite does not
import em_esphome (zeroconf, aiohttp, the database), and both of the rules
above are invisible at the call site — the code reads fine either way.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import numpy as np

log = logging.getLogger("echomuse.announce")

# Whole-announcement cap: fetch plus playback. Sized to sit well under HA's
# _ANNOUNCEMENT_TIMEOUT_SEC (5 minutes) so WE are the side that gives up and
# replies, rather than leaving HA holding _is_announcing.
#
# The layer below is already bounded (audio_duration * 2 + 10s in
# em_controller._run_post_turn_playback), which is what the layer below always
# looks like.
ANNOUNCE_TIMEOUT_S = 120.0


# How much quiet a message ends on when the Echo listens straight after it.
# Ours to set, not the text-to-speech engine's (Wil, 2026-10-05): engines pad
# their clips by whatever they like, and that pad is time the person waits
# before they can answer, with the ring dark. A short pause rather than none:
# a reply that listens the instant its last word ends reads as cutting in.
LISTEN_TAIL_MS = 200
# A frame this far below the clip's loudest counts as quiet. Relative, so a
# quiet voice is not taken for silence and a noisy pad is still found.
_QUIET_DB = -40.0
_TAIL_FRAME_MS = 10


def set_trailing_quiet(pcm: bytes, rate: int, tail_ms: int = LISTEN_TAIL_MS) -> bytes:
    """
    `pcm` (mono S16_LE) ending on exactly `tail_ms` of quiet after its last
    sound: a longer tail is cut, a shorter one is filled out with silence.

    What quiet the clip already has is kept up to that length, so the voice's
    own decay is not replaced by a hard cut to zero. A clip with no sound in
    it comes back untouched: there is no end of speech to measure from.
    """
    frame = max(1, rate * _TAIL_FRAME_MS // 1000)
    samples = np.frombuffer(pcm[:len(pcm) - len(pcm) % 2], dtype="<i2")
    n = len(samples) // frame
    if n == 0:
        return pcm
    energy = (samples[:n * frame].astype(np.float64).reshape(n, frame) ** 2).mean(axis=1)
    loudest = float(energy.max())
    if loudest <= 0.0:
        return pcm
    loud = np.nonzero(energy >= loudest * 10.0 ** (_QUIET_DB / 10.0))[0]
    want = (int(loud[-1]) + 1) * frame + rate * tail_ms // 1000
    if want <= len(samples):
        return samples[:want].tobytes()
    return samples.tobytes() + bytes(2 * (want - len(samples)))


async def run(
    media_id: str,
    fetch: Callable[[str], Awaitable[bytes]],
    play: Optional[Callable[[bytes], Awaitable[None]]],
    on_finished: Callable[[bool], None],
    log_name: str = "",
    timeout: float = ANNOUNCE_TIMEOUT_S,
    preannounce_media_id: str = "",
    tail: Optional[Callable[[bytes], bytes]] = None,
) -> bool:
    """
    Fetch the announcement audio, play it, then report completion exactly once.

    `play` is None when nothing can play it — the physical device is not
    connected. That is not a successful announcement and HA is not told it was.

    `on_finished` is called from the finally on every path, including the ones
    that raise. It is a plain callable rather than a coroutine because the
    caller's job here is a socket write it may also decline to make (a closed
    transport), and awaiting a decision not to send is noise.

    `preannounce_media_id` is the attention chime HA plays BEFORE the message,
    on `VoiceAssistantAnnounceRequest` field 3. It matters most on
    `start_conversation`, where an unprompted "the garage door is open" arrives
    with no warning at all — nobody asked a question, so there is nothing else
    to tell the listener that the device is about to talk and then listen.

    **A preannounce failure is not an announcement failure.** The chime is a
    cue for the message; a missing cue is worth a log line, not a swallowed
    announcement, and `ok` reports the MESSAGE. Both share one timeout budget
    so a wedged chime cannot extend the whole thing past it.

    `tail` reshapes the end of the MESSAGE before it plays, and is passed when
    the Echo will listen straight after (`set_trailing_quiet`). The chime is
    left as it is: nothing waits on its last note.
    """
    ok = False
    try:
        if not media_id:
            log.warning(f"[{log_name}] AnnounceRequest with no media_id")
        else:
            ok = await asyncio.wait_for(
                _preannounce_then_play(
                    media_id, preannounce_media_id, fetch, play, log_name, tail),
                timeout)
    except (asyncio.TimeoutError, TimeoutError):
        log.error(f"[{log_name}] Announce timed out after {timeout}s")
    except Exception as e:
        log.error(f"[{log_name}] Announce fetch/play error: {e}")
    finally:
        on_finished(ok)
    return ok


async def _preannounce_then_play(
    media_id: str,
    preannounce_media_id: str,
    fetch: Callable[[str], Awaitable[bytes]],
    play: Optional[Callable[[bytes], Awaitable[object]]],
    log_name: str = "",
    tail: Optional[Callable[[bytes], bytes]] = None,
) -> bool:
    """The chime, then the message. Returns whether the MESSAGE played."""
    if preannounce_media_id:
        try:
            await play_media(preannounce_media_id, fetch, play, log_name)
        except Exception as e:
            log.warning(f"[{log_name}] Preannounce chime failed: {e}")
    return await play_media(media_id, fetch, play, log_name, tail)


async def play_media(
    media_id: str,
    fetch: Callable[[str], Awaitable[bytes]],
    play: Optional[Callable[[bytes], Awaitable[object]]],
    log_name: str = "",
    tail: Optional[Callable[[bytes], bytes]] = None,
) -> bool:
    """
    Fetch and play, with no reply to anyone. True if the audio reached the
    speaker.

    Public because HA has TWO ways to announce and only one of them waits for
    a completion message: `VoiceAssistantAnnounceRequest` (run(), above) and
    `play_media` with announce=true, which is an ordinary media_player command.
    Sending AnnounceFinished for the latter would answer a question nobody
    asked.

    A `play` callback returning False means the audio did not reach the
    speaker — cancelled by a mute or a button mid-playback. None (the common
    case) means it has no opinion and is taken as played.
    """
    pcm_bytes = await fetch(media_id)
    if not pcm_bytes:
        log.warning(f"[{log_name}] Announce fetched no audio")
        return False

    if play is None:
        log.info(
            f"[{log_name}] Announce audio fetched ({len(pcm_bytes)}b) "
            f"— no playback callback set (standalone announce)"
        )
        return False

    if tail is not None:
        before = len(pcm_bytes)
        pcm_bytes = tail(pcm_bytes)
        if len(pcm_bytes) != before:
            log.info(f"[{log_name}] Announce tail set: "
                     f"{len(pcm_bytes) - before:+d} bytes")
    return await play(pcm_bytes) is not False
