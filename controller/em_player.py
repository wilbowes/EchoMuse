"""
em_player.py — media playback sessions (music on the Echo's speaker)
=====================================================================

Turns the HA `media_player` entity from a voice-pipeline facade into a
real player: `media_player.play_media` (and anything built on it — Music
Assistant, HA's media browser, radio) streams through the controller to
the device's existing 0x02 speaker plane.

Design constraints this encodes:

- **Streaming decode.** TTS clips are fully decoded then streamed; music
  can be minutes long or a live radio stream with no end, so ffmpeg runs
  as a subprocess piping s16le/48k/mono to us and we forward as it
  arrives.
- **Realtime pacing with a small lead.** The device buffers ~5.5s and
  the turn path exploits that by writing far ahead of realtime. A music
  feed that ran ahead like that would make pause/stop laggy (whatever
  is buffered still plays) — so the feed keeps only ~1.5s of lead:
  enough to ride WiFi hiccups, small enough that a flush feels instant.
- **Pause = flush + position bookmark.** The wire has no device-side
  pause; pausing cancels the feed (its EOS goes out first — the flush
  discard arms until EOS, exactly the barge-in dance), flushes the
  device buffer, and remembers the play position. Resume restarts
  ffmpeg with `-ss` a moment before the bookmark (non-seekable live
  streams just rejoin the live edge — ffmpeg ignores -ss it can't do).
- **Voice preempts music.** interrupt()/resume_interrupted() bracket
  voice turns and announcements: an active session pauses for the
  duration and resumes afterwards. The wake stream stays live during
  music (the feed deliberately does NOT set device.speaking — that flag
  makes the wake loop drop frames, which would leave the device deaf
  for a whole song); wake-over-music relies on AEC, and the wake loop
  scores against bargeInThreshold while music plays for the same
  reason barge-in does during TTS.

The controller injects its device registry and an HA state-push callback
via init() — this module imports only em_eq, so it stays unit-testable
(tests stub the decoder and the device).
"""

from __future__ import annotations

import asyncio
import logging

import em_eq

log = logging.getLogger("player")

SPEAKER_RATE  = 48000
SPEAKER_BYTES = 4096                     # bytes per 0x02 period (mono S16)
BYTES_PER_SEC = SPEAKER_RATE * 2
SPEAKER_FRAME_TYPE = 0x02
SPEAKER_EOS_TYPE   = 0x03

LEAD_S          = 1.5   # feed-ahead target over realtime
RESUME_REWIND_S = 1.0   # replay this much before the pause bookmark
DRAIN_FUDGE_S   = 1.1   # device prime hold — same constant class as turns

IDLE, PLAYING, PAUSED = "idle", "playing", "paused"

# Injected by em_controller.init(): device_id -> Device | None
_get_device = None
# Injected: async callable(device_id, state_str) — pushes the new state to
# HA via the ESPHome satellite. Best-effort; None until wired.
_notify_state = None

_sessions: dict[str, "MediaSession"] = {}


def init(get_device, notify_state) -> None:
    global _get_device, _notify_state
    _get_device = get_device
    _notify_state = notify_state


def _session(device_id: str) -> "MediaSession":
    s = _sessions.get(device_id)
    if s is None:
        s = _sessions[device_id] = MediaSession(device_id)
    return s


def state(device_id: str) -> str:
    s = _sessions.get(device_id)
    return s.state if s else IDLE


def is_playing(device_id: str) -> bool:
    return state(device_id) == PLAYING


async def play(device_id: str, url: str) -> None:
    await _session(device_id).play(url)


async def pause(device_id: str) -> None:
    await _session(device_id).pause()


async def resume(device_id: str) -> None:
    await _session(device_id).resume()


async def stop(device_id: str) -> None:
    await _session(device_id).stop()


async def interrupt(device_id: str) -> None:
    """Voice turn / announcement starting — pause an active session."""
    s = _sessions.get(device_id)
    if s is not None and s.state == PLAYING:
        s.interrupted = True
        await s.pause()


async def resume_interrupted(device_id: str) -> None:
    """Voice turn / announcement over — resume iff *we* paused it."""
    s = _sessions.get(device_id)
    if s is not None and s.interrupted:
        s.interrupted = False
        if s.state == PAUSED:
            await s.resume()


def device_gone(device_id: str) -> None:
    """Device disconnected — kill any session without touching the wire."""
    s = _sessions.pop(device_id, None)
    if s is not None:
        s.abandon()


class MediaSession:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.state = IDLE
        self.url: str | None = None
        self.interrupted = False   # paused by voice, not by the user
        self._pos = 0.0            # seconds into the media at last pause
        self._task: asyncio.Task | None = None
        self._proc = None

    # ── decoder (stubbed in tests) ────────────────────────────────────────

    async def _spawn_decoder(self, url: str, position_s: float):
        """ffmpeg → s16le/48k/mono on stdout. Returns the process."""
        args = ["ffmpeg", "-nostdin", "-loglevel", "error"]
        if position_s > 0.5:
            args += ["-ss", f"{position_s:.2f}"]
        args += ["-i", url, "-f", "s16le", "-acodec", "pcm_s16le",
                 "-ac", "1", "-ar", str(SPEAKER_RATE), "-"]
        return await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    # ── controls ──────────────────────────────────────────────────────────

    async def play(self, url: str) -> None:
        await self.stop()
        self.url = url
        self._pos = 0.0
        self._start_feed()

    async def pause(self) -> None:
        if self.state != PLAYING:
            return
        await self._halt(bookmark=True)
        self.state = PAUSED
        log.info(f"[{self.device_id}] Media paused at {self._pos:.1f}s")
        await self._push_state()

    async def resume(self) -> None:
        if self.state != PAUSED or self.url is None:
            return
        self._start_feed()

    async def stop(self) -> None:
        was_active = self.state != IDLE
        await self._halt(bookmark=False)
        self.state = IDLE
        self.url = None
        self._pos = 0.0
        self.interrupted = False
        if was_active:
            log.info(f"[{self.device_id}] Media stopped")
            await self._push_state()

    def abandon(self) -> None:
        """Device is gone — drop the feed without any wire traffic."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.state = IDLE

    # ── internals ─────────────────────────────────────────────────────────

    def _start_feed(self) -> None:
        self.state = PLAYING
        self._task = asyncio.create_task(self._feed())
        log.info(
            f"[{self.device_id}] Media playing: {self.url!r} "
            f"(from {self._pos:.1f}s)"
        )

    async def _halt(self, bookmark: bool) -> None:
        """
        Tear down an active feed. Order matters and mirrors barge-in:
        speaker_flush first (arms the device's discard-until-EOS + drains
        its buffer), then cancel the feed — whose finally sends the EOS
        that disarms the discard.
        """
        task = self._task
        self._task = None
        if task is None or task.done():
            if bookmark and self.state == PLAYING:
                pass  # feed finished by itself; position already final
            return
        device = _get_device(self.device_id) if _get_device else None
        if device is not None:
            try:
                await device.send_control({"type": "speaker_flush"})
            except Exception as e:
                log.warning(f"[{self.device_id}] speaker_flush failed: {e}")
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _push_state(self) -> None:
        if _notify_state is not None:
            try:
                await _notify_state(self.device_id, self.state)
            except Exception as e:
                log.warning(f"[{self.device_id}] media state push failed: {e}")

    async def _feed(self) -> None:
        loop = asyncio.get_running_loop()
        device = _get_device(self.device_id) if _get_device else None
        if device is None:
            log.warning(f"[{self.device_id}] Media play: device not connected")
            self.state = IDLE
            await self._push_state()
            return

        eq = em_eq.StreamingEQ(SPEAKER_RATE, device.eq_bands, device.eq_loudness)
        start_pos = self._pos
        proc = None
        seg_start = loop.time()
        sent = 0
        eos_sent = False
        try:
            proc = await self._spawn_decoder(self.url, start_pos)
            self._proc = proc
            seg_start = loop.time()
            await self._push_state()

            while True:
                try:
                    chunk = await proc.stdout.readexactly(SPEAKER_BYTES)
                except asyncio.IncompleteReadError as e:
                    chunk = e.partial
                    if chunk:
                        chunk = chunk + bytes(SPEAKER_BYTES - len(chunk))
                        await device.send_data(
                            bytes([SPEAKER_FRAME_TYPE]) + eq.process(chunk))
                        sent += SPEAKER_BYTES
                    break
                # Pacing: hold the lead at LEAD_S over realtime so a flush
                # (pause/stop/voice preempt) feels instant.
                ahead = sent / BYTES_PER_SEC - (loop.time() - seg_start)
                if ahead > LEAD_S:
                    await asyncio.sleep(ahead - LEAD_S)
                await device.send_data(
                    bytes([SPEAKER_FRAME_TYPE]) + eq.process(chunk))
                sent += SPEAKER_BYTES

            # Natural end of media: close the stream and wait out the
            # device buffer before reporting idle.
            await device.send_data(bytes([SPEAKER_EOS_TYPE]))
            eos_sent = True
            remaining = (sent / BYTES_PER_SEC
                         - (loop.time() - seg_start) + DRAIN_FUDGE_S)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self.state = IDLE
            self.url = None
            self._pos = 0.0
            log.info(f"[{self.device_id}] Media finished "
                     f"({sent // SPEAKER_BYTES} periods)")
            await self._push_state()
        except asyncio.CancelledError:
            # pause()/stop() tearing us down. Bookmark ≈ what has audibly
            # played: the device plays realtime once primed, so wall time
            # since segment start, capped by what we actually sent.
            played = min(sent / BYTES_PER_SEC, loop.time() - seg_start)
            self._pos = max(0.0, start_pos + played - RESUME_REWIND_S)
            raise
        except Exception as e:
            log.error(f"[{self.device_id}] Media feed error: {e}")
            self.state = IDLE
            await self._push_state()
        finally:
            if not eos_sent:
                # The flush discard stays armed until it sees this stream's
                # EOS — same contract as barge-in aborting stream_speaker.
                try:
                    await asyncio.shield(
                        device.send_data(bytes([SPEAKER_EOS_TYPE])))
                except BaseException:
                    pass
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            self._proc = None
