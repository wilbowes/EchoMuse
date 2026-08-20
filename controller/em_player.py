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
  ffmpeg with `-ss` a moment before the bookmark. ffmpeg does NOT
  ignore a seek it cannot perform — on a non-seekable live stream it
  decodes and discards input until it reaches the timestamp, so a
  173s bookmark means 173s of silence. A first-chunk deadline
  (SEEK_STALL_S) catches that and rejoins the live edge instead.
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
import em_limiter
import em_mbc

log = logging.getLogger("player")

SPEAKER_RATE  = 48000
SPEAKER_BYTES = 4096                     # bytes per 0x02 period (mono S16)
BYTES_PER_SEC = SPEAKER_RATE * 2
SPEAKER_FRAME_TYPE = 0x02
SPEAKER_EOS_TYPE   = 0x03

# Music rides its own plane on firmware that can mix (the "audio_mix"
# capability), so a voice turn ducks it instead of pausing it. On older
# firmware music stays on 0x02 and the pause/resume path is kept — a device
# that cannot mix would never play 0x04 at all, which is silence rather than
# degraded behaviour.
MUSIC_FRAME_TYPE = 0x04
MUSIC_EOS_TYPE   = 0x05


def _frame_types(device) -> tuple[int, int]:
    """(audio frame type, EOS type) for this device's music stream."""
    if device is not None and getattr(device, "audio_mix_capable", False):
        return MUSIC_FRAME_TYPE, MUSIC_EOS_TYPE
    return SPEAKER_FRAME_TYPE, SPEAKER_EOS_TYPE

# Feed-ahead target over realtime. The device buffers audioChanDepth=128
# periods x 42.7ms = ~5.46s, so 1.5s left roughly four seconds of hardware
# buffer unused — and control-plane RTT measurement shows stalls of 1.8s and
# 2.6s during playback on a healthy-looking link, each of which drained that
# 1.5s and produced an audible dropout (reported repeatedly 2026-07-25).
#
# Raising this does NOT make pause/stop/voice-preempt less responsive: those
# are instant because of speaker_flush (which drains the device buffer) plus
# the discard-until-EOS contract (which swallows whatever is still in TCP),
# not because the lead is short. 4.0s keeps ~1.4s of headroom under the
# device's own depth so the feed can never outrun audioCh.
#
# This is MITIGATION, not a cure: it rides out the stalls rather than
# explaining them. The stalls are still unexplained — see the RTT
# instrumentation and docs/led-ring-states.md-era investigation notes.
LEAD_S          = 4.0

# The lead the music feed drops to while a voice turn owns the speaker.
#
# Music and TTS share ONE /data WebSocket, so a 4s music lead means up to
# ~380KB of music can be queued ahead of the response in the socket. When the
# link hiccups — and this fleet stalls for seconds at a time — the device must
# drain all that music before it reaches the TTS frames. Measured on the first
# ducked turn: the response waited 2087ms to start and then took a 3549ms gap
# mid-stream, against 41ms/0ms on turns where nothing else was streaming.
#
# Dropping the lead makes the feed stop sending until it has drained back to
# TURN_LEAD_S, which clears the queue ahead of the response. The music keeps
# playing throughout, from the buffer the device already holds — that buffer
# is exactly why ducking has to happen device-side, and this is the same
# property paying for itself twice.
#
# Not zero: the music would then be sent frame-by-frame at the instant it is
# needed, with no protection at all against a stall DURING the turn. This
# trades the BACKGROUND's stall margin for the FOREGROUND's, which is the
# right way round — the response is what the user is listening to.
TURN_LEAD_S     = 1.0
RESUME_REWIND_S = 1.0   # replay this much before the pause bookmark
DRAIN_FUDGE_S   = 1.1   # device prime hold — same constant class as turns
# How long to wait for the first decoded audio after a seek before deciding
# the stream cannot actually seek. ffmpeg's -ss is an INPUT seek: on a
# seekable file it is a fast demuxer jump and audio appears at once, but on a
# non-seekable live stream ffmpeg decodes and DISCARDS input until it reaches
# the timestamp — so resuming a 173s bookmark on a continuous stream waits
# ~173s of wall clock producing silence. Generous enough not to fire on a
# slow-starting seekable source.
SEEK_STALL_S    = 5.0

# A single read from ffmpeg taking longer than this is a SOURCE stall: the
# decoder had nothing for us, so nothing went to the device. Logged when it
# happens rather than only summarised, because the timing is the value — the
# same reasoning as shadow threshold crossings.
#
# This exists to answer a question the device cannot: the device reports a gap
# between frame ARRIVING, which looks identical whether the controller had
# nothing to send (source stall, e.g. a Music Assistant flow starving) or the
# link swallowed it. send_ms cannot settle it either, since a socket write
# completes near-instantly however slow the wire is. So: a device-side gap WITH
# a source stall at the same moment is upstream; without one it is the link.
#
# 500ms is well inside the ~4s lead, so a stall this size is not yet audible —
# which is the point of catching it here.
SOURCE_STALL_MS = 500.0


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


def reported_state(device_id: str) -> str:
    """
    What Home Assistant should believe, which is deliberately NOT always our
    internal state.

    While a voice turn owns the speaker we pause the wire, but that pause is
    OURS and invisible to the user — from where they are standing the music is
    playing and the assistant is talking over it. Home Assistant must keep
    seeing PLAYING for the length of the turn.

    This is #62. Reporting the internal PAUSED made Home Assistant answer
    "it's already paused" to a spoken pause request and **never send the
    command** — so `pause()` below was never called, no intent was recorded,
    and `resume_interrupted` then put the music back. The user's pause was
    silently dropped and then contradicted, which is exactly what was
    reported. Note the entity was never *wrong* about our state machine; it
    was wrong about what the user could see, and HA acts on the latter.

    Once the user does issue a command during the turn, `pending` holds it and
    the real state is reported again — by then the entity state agrees with
    something the user actually asked for.
    """
    s = _sessions.get(device_id)
    if s is None:
        return IDLE
    if s.owned_by_turn and s.resume_after and s.pending is None:
        return PLAYING
    return s.state


async def play(device_id: str, url: str) -> None:
    s = _session(device_id)
    if s.owned_by_turn:
        # The common collision: "play some jazz" runs the intent BEFORE Home
        # Assistant generates the spoken reply, so this can land while the TTS
        # is still coming and put music on the same 0x02 plane as the response.
        s.pending = ("play", url)
        await s.push_intent(PLAYING)
        return
    await s.play(url)


async def pause(device_id: str) -> None:
    """Pause from the USER (HA / Music Assistant / the media_player entity)."""
    s = _session(device_id)
    if s.owned_by_turn:
        # Already paused on the wire by interrupt(); record that the user now
        # OWNS the paused state so the turn's end does not undo it.
        s.pending = ("pause", None)
        # Push it: until now HA was told PLAYING (see reported_state), because
        # our own interrupt-pause must not read as the user's. Now that they
        # have asked, the entity should say so without waiting for turn end.
        await s.push_intent(PAUSED)
        return
    await s.pause()


async def resume(device_id: str) -> None:
    s = _session(device_id)
    if s.owned_by_turn:
        s.pending = ("resume", None)
        await s.push_intent(PLAYING)
        return
    await s.resume()


async def stop(device_id: str) -> None:
    s = _session(device_id)
    if s.owned_by_turn:
        s.pending = ("stop", None)
        await s.push_intent(IDLE)
        return
    await s.stop()


async def interrupt(device_id: str) -> None:
    """
    A voice turn or announcement is starting: it OWNS the speaker until it
    ends.

    Ownership is taken unconditionally, even with nothing playing. That is the
    point: interrupt() used to only pause what was already playing, which did
    nothing to stop something STARTING mid-turn — and "play some jazz" runs
    the intent before Home Assistant generates the spoken reply, so play_media
    can arrive while the TTS is still coming and put music on the same 0x02
    plane as the response.

    resume_after is separate from ownership on purpose: it records only that
    WE paused something and should put it back. A user command during the
    turn replaces that intent (see resume_interrupted).
    """
    s = _session(device_id)
    s.owned_by_turn = True
    s.pending = None

    device = _get_device(device_id) if _get_device else None
    if device is not None and getattr(device, "audio_mix_capable", False):
        # DUCK, do not pause. The device holds music on its own plane and
        # mixes it under the response, so the music keeps playing quietly and
        # nothing is lost: no seek, no bookmark, no position drift on a
        # non-seekable flow stream, and no need for the entity to claim it is
        # playing while internally paused.
        s.ducked = True
        # Yield the shared data plane to the response. The music keeps
        # playing from the device's own buffer while the feed holds off.
        s.lead_s = TURN_LEAD_S
        try:
            await device.send_control({"type": "duck", "on": True})
        except Exception as e:
            log.warning(f"[{device_id}] duck failed: {e}")
        return

    if s.state == PLAYING:
        s.resume_after = True
        await s.pause()


async def resume_interrupted(device_id: str) -> None:
    """
    The turn is over: release the speaker and settle what should be playing.

    A command issued during the turn wins over our auto-resume — it is the
    user's instruction, ours is bookkeeping. Last write wins among several,
    which is what "play jazz... actually, pause" means.

    This is where the pause bug in #53 is really fixed: asking to pause during
    a barge-in used to be discarded (MediaSession.pause() returns early unless
    PLAYING) and then contradicted by the auto-resume.
    """
    s = _sessions.get(device_id)
    if s is None:
        return
    s.owned_by_turn = False
    pending, s.pending = s.pending, None
    resume_after, s.resume_after = s.resume_after, False
    ducked, s.ducked = s.ducked, False
    s.lead_s = LEAD_S

    if ducked:
        # Release the duck before settling any deferred command, so the music
        # is already back at level by the time a stop or pause lands.
        device = _get_device(device_id) if _get_device else None
        if device is not None:
            try:
                await device.send_control({"type": "duck", "on": False})
            except Exception as e:
                log.warning(f"[{device_id}] unduck failed: {e}")

    if pending is not None:
        kind, url = pending
        if kind == "play":
            await s.play(url)
        elif kind == "resume":
            await s.resume()
        elif kind == "stop":
            await s.stop()
        elif kind == "pause" and ducked:
            # When we ducked, nothing was ever paused — so the user's pause
            # has to actually happen here. On the pausing path interrupt()
            # had already done it and dropping resume_after was the whole of
            # what the user asked for.
            await s.pause()
        return

    if resume_after and s.state == PAUSED:
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
        # A voice turn or announcement owns the speaker right now, so
        # playback commands are recorded rather than put on the wire.
        self.owned_by_turn = False
        # The user's last playback command during that ownership, applied when
        # it is released. Last write wins.
        self.pending: tuple[str, str | None] | None = None
        # WE paused something and should put it back afterwards — distinct
        # from ownership, and overridden by any pending user command.
        self.resume_after = False
        # ducked: this turn lowered the music rather than pausing it (a device
        # that mixes). It changes what turn end has to do — there is nothing
        # to resume, but a deferred pause must actually be carried out.
        self.ducked = False
        # Effective pacing lead. Reduced while a turn owns the speaker so the
        # music feed stops monopolising the shared data plane (see
        # TURN_LEAD_S); restored when the turn releases it.
        self.lead_s = LEAD_S
        self._pos = 0.0            # seconds into the media at last pause
        self._task: asyncio.Task | None = None
        self._proc = None
        # Whether this URL can be seeked. Learned, not guessed: the first
        # failed resume costs SEEK_STALL_S of silence to discover, and every
        # later resume on the same stream would pay it again for an answer we
        # already have. A Music Assistant flow is the common case — barge in
        # three times during a song and that is 15s of dead air.
        self._seekable = True

    # ── decoder (stubbed in tests) ────────────────────────────────────────

    @staticmethod
    async def _kill_decoder(proc) -> None:
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass

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
        # New URL, new answer — a track file after a flow stream is seekable.
        self._seekable = True
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
        self.resume_after = False
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
        # t0 for the playback chain trace: the moment we were ASKED to play,
        # so every later stage can be reported as a delta from the command.
        self._t_play = asyncio.get_event_loop().time()
        self.state = PLAYING
        self._task = asyncio.create_task(self._feed())
        log.info(
            f"[{self.device_id}] Media playing: {self.url!r} "
            f"(from {self._pos:.1f}s)"
        )

    async def _halt(self, bookmark: bool) -> None:
        """
        Tear down an active feed. Order matters and mirrors barge-in: flush
        first (arms the device's discard-until-EOS + drains its buffer), then
        cancel the feed — whose finally sends the EOS that disarms the
        discard.

        On a device that mixes, the flush must target the MUSIC plane:
        speaker_flush would cut the voice stream and leave the music playing,
        which is precisely backwards. This path is reached only when playback
        genuinely ends — stop, pause, or a new track — never for a voice
        turn, which ducks.
        """
        task = self._task
        self._task = None
        if task is None or task.done():
            if bookmark and self.state == PLAYING:
                pass  # feed finished by itself; position already final
            return
        device = _get_device(self.device_id) if _get_device else None
        if device is not None:
            flush = ("music_flush" if getattr(device, "audio_mix_capable", False)
                     else "speaker_flush")
            try:
                await device.send_control({"type": flush})
            except Exception as e:
                log.warning(f"[{self.device_id}] {flush} failed: {e}")
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def push_intent(self, state: str) -> None:
        """
        Tell Home Assistant what WILL be true once the turn releases the
        speaker, without lying to our own state machine.

        Deferring a command would otherwise leave the media_player entity
        showing stale state for the length of a turn — and #53's other half is
        already a complaint about that entity being wrong. The feed pushes the
        authoritative state as soon as it actually starts, exactly as
        play_media has always been optimistic.
        """
        if _notify_state is not None:
            try:
                await _notify_state(self.device_id, state)
            except Exception as e:
                log.warning(f"[{self.device_id}] media intent push failed: {e}")

    async def _push_state(self) -> None:
        # reported_state, not self.state: an interrupt-pause during a turn
        # must not reach HA as a real pause (#62).
        if _notify_state is not None:
            try:
                await _notify_state(self.device_id, reported_state(self.device_id))
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

        # Resolved once per feed rather than per chunk: the device cannot
        # gain or lose the capability mid-stream, and this runs ~23×/s.
        frame_type, eos_type = _frame_types(device)

        # Built once for the whole feed and then UPDATED in place, never
        # rebuilt: both processors carry filter and gain state, so a new
        # instance mid-track restarts the crossover and clicks. Constructed
        # even when disabled — a bypassed instance keeps its state warm, which
        # is what makes toggling one mid-song silent.
        #
        # In place because these are taste parameters tuned by ear in a real
        # room. Reading them once per stream meant every A/B cost a track
        # skip, which is long enough that nobody can hold the two in their
        # head (measured against a listening test on 2026-08-19: no audible
        # difference reported, because none of the changes were reaching the
        # audio at all).
        eq = em_eq.StreamingEQ(SPEAKER_RATE, device.eq_bands, device.eq_loudness,
                               limiter=em_limiter.Limiter(
                                   SPEAKER_RATE,
                                   threshold_db=device.limiter_threshold,
                                   release_ms=device.limiter_release,
                                   enabled=device.limiter_enabled),
                               guard=em_mbc.BassGuard(
                                   SPEAKER_RATE,
                                   bass_guard_db=device.bass_guard_db,
                                   enabled=device.bass_guard_enabled))
        start_pos = self._pos
        proc = None
        seg_start = loop.time()
        sent = 0
        eos_sent = False
        src_max_ms = 0.0
        src_stalls = 0

        # Arm the data-plane reconnect grace for this feed: a Wi-Fi blip
        # mid-song should cost a pause, not the rest of the track.
        if hasattr(device, "begin_data_stream"):
            device.begin_data_stream()

        # Already known to be unseekable: go straight to the live edge rather
        # than spending SEEK_STALL_S of silence rediscovering it.
        if start_pos > 0.5 and not self._seekable:
            log.info(
                f"[{self.device_id}] Stream is not seekable — resuming at the "
                f"live edge instead of {start_pos:.1f}s"
            )
            start_pos = self._pos = 0.0

        t0 = getattr(self, "_t_play", None) or loop.time()
        t_spawned = None
        t_first_pcm = None
        t_first_send = None

        try:
            proc = await self._spawn_decoder(self.url, start_pos)
            self._proc = proc
            t_spawned = loop.time()

            # A seek we cannot actually perform produces no audio at all
            # rather than failing loudly (see SEEK_STALL_S). Give the first
            # chunk a deadline and, if it does not arrive, rejoin the live
            # edge — for a continuous stream that IS the right resume point,
            # and it is strictly better than silence. Observed 2026-07-25:
            # a voice turn over a Music Assistant flow stream resumed at
            # 173.6s and the device stayed silent while HA showed playing.
            pending = None
            if start_pos > 0.5:
                try:
                    pending = await asyncio.wait_for(
                        proc.stdout.readexactly(SPEAKER_BYTES), SEEK_STALL_S)
                except asyncio.TimeoutError:
                    log.warning(
                        f"[{self.device_id}] No audio {SEEK_STALL_S:.0f}s after "
                        f"seeking to {start_pos:.1f}s — stream is not seekable; "
                        f"rejoining the live edge"
                    )
                    await self._kill_decoder(proc)
                    self._seekable = False
                    start_pos = self._pos = 0.0
                    proc = await self._spawn_decoder(self.url, 0.0)
                    self._proc = proc
                except asyncio.IncompleteReadError as e:
                    pending = (e.partial + bytes(SPEAKER_BYTES - len(e.partial))
                               ) if e.partial else None

            seg_start = loop.time()
            await self._push_state()

            while True:
                # Config is pushed live (_apply_live_config), so re-read it
                # per chunk. update() compares before it touches anything, so
                # the steady-state cost is a tuple comparison ~23×/s.
                #
                # Logged whenever it MOVES, which is once at the start of the
                # stream and once per dashboard change. That line is the only
                # proof that a setting reached the audio: the stages cancel
                # each other's most obvious cue, so "I heard nothing" cannot
                # distinguish a working chain from a config that never
                # arrived. See em_eq.describe_chain.
                if eq.update(bands=device.eq_bands,
                             loudness=device.eq_loudness,
                             limiter_enabled=device.limiter_enabled,
                             limiter_threshold=device.limiter_threshold,
                             limiter_release=device.limiter_release,
                             guard_enabled=device.bass_guard_enabled,
                             guard_db=device.bass_guard_db):
                    log.info(f"[{self.device_id}] Output chain: "
                             f"{em_eq.describe_chain(device.eq_bands, device.eq_loudness, eq.limiter, eq.guard)}")

                try:
                    if pending is not None:
                        chunk, pending = pending, None
                    else:
                        # Only the READ is timed. The pacing sleep below is
                        # deliberate and must not read as a stall.
                        _t0 = loop.time()
                        chunk = await proc.stdout.readexactly(SPEAKER_BYTES)
                        _read_ms = (loop.time() - _t0) * 1000.0
                        if t_first_pcm is None:
                            t_first_pcm = loop.time()
                        if _read_ms > src_max_ms:
                            src_max_ms = _read_ms
                        if _read_ms > SOURCE_STALL_MS:
                            src_stalls += 1
                            log.warning(
                                f"[{self.device_id}] Media SOURCE stall: "
                                f"{_read_ms:.0f}ms with no audio from the decoder "
                                f"({sent / BYTES_PER_SEC:.1f}s sent) — upstream, "
                                f"not the device link")
                except asyncio.IncompleteReadError as e:
                    chunk = e.partial
                    if chunk:
                        chunk = chunk + bytes(SPEAKER_BYTES - len(chunk))
                        await device.send_data(
                            bytes([frame_type]) + eq.process(chunk))
                        sent += SPEAKER_BYTES
                    break
                # Pacing. Read per chunk, not captured once: a voice turn
                # lowers the lead mid-stream so the response gets the wire,
                # and that has to take effect on the next frame rather than
                # the next track.
                lead = self.lead_s
                ahead = sent / BYTES_PER_SEC - (loop.time() - seg_start)
                if ahead > lead:
                    await asyncio.sleep(ahead - lead)
                await device.send_data(
                    bytes([frame_type]) + eq.process(chunk))
                if t_first_send is None:
                    t_first_send = loop.time()
                    # The chain, as deltas from the play command. Without this
                    # a slow start was unattributable: "Media playing" is
                    # logged the instant we are asked, and the next line is the
                    # stream ENDING, so everything between was invisible.
                    def _ms(t):
                        return f"{(t - t0) * 1000:.0f}ms" if t else "n/a"
                    log.info(
                        f"[{self.device_id}] Playback chain: decoder spawned "
                        f"{_ms(t_spawned)}, first audio out of ffmpeg "
                        f"{_ms(t_first_pcm)}, first frame to device "
                        f"{_ms(t_first_send)} — device then buffers to "
                        f"primePeriods before its ALSA write begins")
                sent += SPEAKER_BYTES

            # Natural end of media: close the stream and wait out the
            # device buffer before reporting idle.
            await device.send_data(bytes([eos_type]))
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
            # Summary on every path out, so a stream that was paused or
            # stopped reports as readily as one that finished. Pair it with
            # the device's own "[speaker] stream complete ... maxGap=" line:
            # a device-side gap with no source stall here means the link,
            # a device-side gap WITH one means upstream.
            if sent:
                log.info(
                    f"[{self.device_id}] Media feed done: "
                    f"{sent // SPEAKER_BYTES} periods, source max read "
                    f"{src_max_ms:.0f}ms, {src_stalls} stall(s) over "
                    f"{SOURCE_STALL_MS:.0f}ms, "
                    f"{em_eq.describe_activity(eq.limiter, eq.guard)}")
            if not eos_sent:
                # The flush discard stays armed until it sees this stream's
                # EOS — same contract as barge-in aborting stream_speaker.
                try:
                    await asyncio.shield(
                        device.send_data(bytes([eos_type])))
                except BaseException:
                    pass
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            self._proc = None
