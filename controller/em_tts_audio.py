"""
em_tts_audio.py — Home Assistant's TTS audio, fetched and decoded to wire PCM

Moved out of em_esphome unchanged (2026-11-12): the voice turn streams a
reply with stream(), and announcements fetch one whole with fetch(). Both
give 48 kHz mono S16_LE, the rate the device plays, so nothing downstream
resamples.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import AsyncIterator

import em_wav

log = logging.getLogger("echomuse.esphome")

# The rate the device plays: TTS is asked for, and decoded to, exactly this.
WIRE_RATE = 48000


async def stream(url: str) -> AsyncIterator[bytes]:
    """
    Incrementally fetch and decode HA TTS audio to 48kHz mono S16_LE PCM.

    The HTTP response is piped into one long-lived ffmpeg process while decoded
    PCM is yielded immediately. Neither the encoded response nor decoded speech
    is accumulated in memory. A retry is safe only before PCM has been emitted;
    retrying later would repeat audio the user has already heard.
    """
    emitted = False
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            # aclosing, as below: a barge-in closes this generator, and the
            # inner one must tear ffmpeg down NOW, not when collected.
            async with contextlib.aclosing(_stream_once(url)) as once:
                async for pcm in once:
                    emitted = True
                    yield pcm
            return
        except Exception as err:
            last_exc = err
            if attempt == 0 and not emitted:
                log.warning(
                    f"TTS stream failed before playback ({err}) "
                    "— retrying once"
                )
                await asyncio.sleep(0.5)
                continue
            raise

    raise last_exc


async def _stream_once(url: str) -> AsyncIterator[bytes]:
    """Run one HTTP streaming attempt for stream().

    A WAV already at the wire format (what we declare to HA — see
    supported_formats) is passed straight through: no decoder, so nothing is
    held back when HA pauses between sentences (em_wav). Anything else is
    decoded by ffmpeg.
    """
    import aiohttp

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=10,
        sock_connect=10,
        sock_read=60,
    )
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            chunks = resp.content.iter_chunked(16 * 1024)
            head = bytearray()
            wav = em_wav.WavStream()
            pcm = b""
            try:
                async for chunk in chunks:
                    head += chunk
                    if len(head) >= 4 and head[:4] != b"RIFF":
                        break
                    pcm = wav.push(chunk)
                    if wav.ready:
                        break
            except em_wav.NotWav:
                pass

            if wav.ready and em_wav.is_wire_pcm(wav.format, WIRE_RATE):
                log.debug("TTS: WAV passthrough")
                odd = b""
                async for part in _prepend(pcm, chunks):
                    part = odd + part
                    cut = len(part) & ~1
                    odd = part[cut:]
                    if cut:
                        yield part[:cut]
                return

            log.info(f"TTS: decoding with ffmpeg ({wav.format or 'not WAV'})")
            # aclosing: a barge-in closes THIS generator, and an inner one left
            # to the garbage collector would run its kill-first teardown late,
            # leaving ffmpeg alive past the turn.
            async with contextlib.aclosing(
                    _ffmpeg_decode(_prepend(bytes(head), chunks))) as decoded:
                async for pcm in decoded:
                    yield pcm


async def _prepend(first: bytes, rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    if first:
        yield first
    async for chunk in rest:
        yield chunk


async def _ffmpeg_decode(encoded: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Decode any format ffmpeg reads to wire PCM, yielding as it decodes.

    `-threads 1`: frame-threaded decoding holds one frame per thread before it
    emits anything, which on an 8-core host kept ~1.7s of FLAC inside ffmpeg
    (0.9s with one thread) — audio that is then stranded whenever the input
    pauses. See em_wav for why TTS normally avoids this path altogether.
    """
    proc: asyncio.subprocess.Process | None = None
    feeder: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-threads", "1",
            "-i", "pipe:0",
            "-f", "s16le", "-ar", str(WIRE_RATE), "-ac", "1",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _feed_encoded_audio() -> None:
            assert proc is not None and proc.stdin is not None
            try:
                async for chunk in encoded:
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            finally:
                if not proc.stdin.is_closing():
                    proc.stdin.close()
                    await proc.stdin.wait_closed()

        feeder = asyncio.create_task(_feed_encoded_audio())

        # stderr is drained CONCURRENTLY, not after stdout EOF. A pipe
        # holds ~64KB; if ffmpeg fills it, it blocks writing stderr,
        # stops producing stdout, and the reader below waits forever.
        # -loglevel error keeps that rare, but the case that produces
        # lots of stderr is a malformed or truncated response — exactly
        # the degraded case this path has to survive.
        assert proc.stderr is not None
        stderr_task = asyncio.create_task(proc.stderr.read())

        assert proc.stdout is not None
        while pcm := await proc.stdout.read(16 * 1024):
            yield pcm

        await feeder
        err = await stderr_task
        return_code = await asyncio.wait_for(proc.wait(), timeout=15.0)
        if return_code != 0:
            raise RuntimeError(
                f"ffmpeg streaming decode failed: {err.decode()[:200]}"
            )
    finally:
        # Kill ffmpeg FIRST, before cancelling the feeder — the ordering is
        # what makes this teardown terminate at all.
        #
        # On a barge-in the consumer stops iterating this generator, so
        # nobody drains ffmpeg's stdout. Its stdout pipe fills, ffmpeg blocks
        # writing, and a blocked ffmpeg stops reading stdin. The feeder is
        # then stuck in drain(), and cancelling it runs its own finally,
        # which awaits `stdin.wait_closed()` — a flush that needs ffmpeg to
        # read and therefore never completes. The gather() below never
        # returns and the kill never happens: teardown hangs, holding the
        # turn. Killing first breaks both pipes, so those awaits raise
        # instead of waiting.
        #
        # It also fixes #252. The InvalidStateError comes from asyncio
        # resolving `_stdin_closed` twice — once when we close stdin, once
        # when the kill tears the transport down. Killing while stdin is
        # still open collapses that to one resolution, and the feeder's
        # close() then finds a transport already closing and skips.
        #
        # Measured over 20 runs of a standalone repro of this shape, with
        # nobody reading stdout: cancel-then-kill hung 20/20 and logged
        # InvalidStateError 20/20; kill-first, 0/20 and 0/20.
        #
        # On the success path proc.wait() has already returned, so
        # returncode is set and nothing is killed.
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        for t in (feeder, stderr_task):
            if t is not None and not t.done():
                t.cancel()
        await asyncio.gather(
            *[t for t in (feeder, stderr_task) if t is not None],
            return_exceptions=True,
        )


async def fetch(url: str) -> bytes:
    """
    Fetch TTS audio from HA and decode to 48kHz mono S16_LE PCM — the
    device wire rate, so downstream (EQ → stream) never resamples.

    Uses ffmpeg subprocess — handles MP3, WAV, FLAC, OGG transparently
    regardless of which TTS provider HA is configured with. When HA honours
    our declared supported_formats the URL already serves 48kHz mono WAV
    and the -ar here is a no-op; otherwise ffmpeg's proper resampler does
    the conversion (better than the numpy linear interpolation this
    replaced, which cost ~2dB above 8kHz).

    Requires ffmpeg in PATH (installed via Dockerfile apt-get).
    """
    import aiohttp
    # One retry on fetch failure — observed intermittent tts_proxy fetch
    # failures (outcome=tts_error, tts_bytes=0) where the URL was valid and
    # the very next turn fetched fine. A single 0.5s-backoff retry converts
    # those from a silent dead turn into ~1s of extra latency.
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession() as session:
                # 60s, not 10s: HA's tts_proxy generates the audio while we
                # hold this GET open, so this is a synthesis deadline, not a
                # download one. Whether that shows up here or in tts_gen
                # depends on the engine — a streaming-capable one hands over
                # the URL early and does the work during the fetch, which is
                # how a long response (a read-aloud story) blew the old 10s
                # cap. Reported by @kopiro in #28.
                #
                # Voice-turn TTS no longer uses this buffered helper, but
                # standalone announcements still do.
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    resp.raise_for_status()
                    audio_bytes = await resp.read()
            break
        except Exception as e:
            last_exc = e
            if attempt == 0:
                log.warning(f"TTS fetch failed ({e}) — retrying once")
                await asyncio.sleep(0.5)
    else:
        raise last_exc

    log.debug(f"TTS fetch: fetched {len(audio_bytes)}b, decoding via ffmpeg")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-f", "s16le", "-ar", str(WIRE_RATE), "-ac", "1",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # C1b fix (2026-07-05 review): the aiohttp fetch above is capped at 10s,
    # but proc.communicate() had no timeout at all — if ffmpeg wedges on
    # malformed/truncated input, this could hang the turn indefinitely with
    # no way out. Cheap insurance: 15s cap, kill the process on timeout.
    try:
        pcm, err = await asyncio.wait_for(
            proc.communicate(input=audio_bytes), timeout=15.0
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("ffmpeg decode timed out after 15s")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {err.decode()[:200]}")

    log.debug(f"TTS fetch: decoded to {len(pcm)}b PCM (48kHz mono S16_LE)")
    return pcm
