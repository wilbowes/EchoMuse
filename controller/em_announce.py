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

log = logging.getLogger("echomuse.announce")

# Whole-announcement cap: fetch plus playback. Sized to sit well under HA's
# _ANNOUNCEMENT_TIMEOUT_SEC (5 minutes) so WE are the side that gives up and
# replies, rather than leaving HA holding _is_announcing.
#
# The layer below is already bounded (audio_duration * 2 + 10s in
# em_controller._run_post_turn_playback), which is what the layer below always
# looks like.
ANNOUNCE_TIMEOUT_S = 120.0


async def run(
    media_id: str,
    fetch: Callable[[str], Awaitable[bytes]],
    play: Optional[Callable[[bytes], Awaitable[None]]],
    on_finished: Callable[[bool], None],
    log_name: str = "",
    timeout: float = ANNOUNCE_TIMEOUT_S,
    preannounce_media_id: str = "",
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
    """
    ok = False
    try:
        if not media_id:
            log.warning(f"[{log_name}] AnnounceRequest with no media_id")
        else:
            ok = await asyncio.wait_for(
                _preannounce_then_play(
                    media_id, preannounce_media_id, fetch, play, log_name),
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
) -> bool:
    """The chime, then the message. Returns whether the MESSAGE played."""
    if preannounce_media_id:
        try:
            await play_media(preannounce_media_id, fetch, play, log_name)
        except Exception as e:
            log.warning(f"[{log_name}] Preannounce chime failed: {e}")
    return await play_media(media_id, fetch, play, log_name)


async def play_media(
    media_id: str,
    fetch: Callable[[str], Awaitable[bytes]],
    play: Optional[Callable[[bytes], Awaitable[object]]],
    log_name: str = "",
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

    return await play(pcm_bytes) is not False
