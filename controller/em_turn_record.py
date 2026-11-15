"""
em_turn_record.py — what one voice turn did, logged and written to the turn table

Moved out of em_esphome unchanged (2026-11-12). TurnTrace times a turn and
emits its [TURN] line; persist_turn writes the row, attaching the playback
stats and the wake comparison that arrive around it; record_dropped_turn
writes the stub for a wake that never reached Home Assistant.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Optional

import em_db as db
import em_recordings

log = logging.getLogger("echomuse.esphome")

# ── Per-turn trace ─────────────────────────────────────────────────────────────
# Collects timestamps and key metrics through a voice turn and emits a single
# structured [TURN] log line at completion. Makes it possible to see at a glance
# where time is going and whether quality correlates with any specific stage.
#
# All times are offsets in milliseconds from t0 (turn start / wake detected).
# -1 means the stage was not reached in this turn.


@dataclasses.dataclass
class TurnTrace:
    trigger:          str   = ""      # "wakeword(0.522)" or "button"
    t0:               float = 0.0     # turn start (time.monotonic())
    t_first_frame_ms: int   = -1      # ms from t0 to first real audio frame
    # ms from t0 to HA's STT_VAD_START. Stays -1 when HA's VAD never engaged,
    # which is the whole point of recording it: that is the turn shape that
    # runs to HA's 15s cap, and until now nothing distinguished it from a turn
    # HA endpointed properly. It is also the number the fallback's grace
    # window should be tuned from — 2.5s is currently set against a single
    # measured turn (1.077s) plus microVAD's floor of ~1.06s.
    t_vad_start_ms:   int   = -1
    t_vad_end_ms:     int   = -1      # ms from t0 to VAD sentinel received
    audio_frames:     int   = 0       # number of PCM frames sent to HA
    t_stt_ms:         int   = -1      # ms from t0 to STT result received
    stt_text:         str   = ""      # STT transcript
    t_tts_url_ms:     int   = -1      # ms from t0 to TTS URL received
    t_tts_fetched_ms: int   = -1      # ms from t0 to TTS audio fetched+decoded
    tts_bytes:        int   = 0       # decoded PCM bytes
    t_playback_ms:    int   = -1      # ms from t0 to playback started
    t_complete_ms:    int   = -1      # ms from t0 to turn complete
    # HA-side segment timings, derived from the marks above at emit time.
    # Broken out because "the turn felt slow" needs to point at a stage:
    # ha_think = HA's own STT+intent time, tts_gen = how long HA took to
    # produce the audio URL after deciding what to say, fetch = our pull
    # of that audio. Together they separate "HA was slow" from "we were".
    ha_think_ms:      int   = -1
    tts_gen_ms:       int   = -1
    fetch_ms:         int   = -1
    outcome:          str   = ""      # "ok", "no_speech", "cancelled", "barged", "tts_error", "timeout"
    # Wake detection detail (model, score, threshold, noise_floor) popped
    # from device.last_wake at turn start — None for button/continuation
    # turns. Persisted with the turn for threshold tuning and model A/Bs.
    wake_info:        Optional[dict] = None

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def mark(self, attr: str) -> None:
        """Record current elapsed time into the named timestamp field."""
        setattr(self, attr, self.elapsed_ms())

    def derive(self) -> None:
        """
        Fill the HA-side segment durations from the absolute marks. Pure
        arithmetic on values already captured — no extra timing calls.
        """
        if self.t_vad_end_ms >= 0 and self.t_stt_ms >= 0:
            self.ha_think_ms = self.t_stt_ms - self.t_vad_end_ms
        if self.t_stt_ms >= 0 and self.t_tts_url_ms >= 0:
            self.tts_gen_ms = self.t_tts_url_ms - self.t_stt_ms
        if self.t_tts_url_ms >= 0 and self.t_tts_fetched_ms >= 0:
            self.fetch_ms = self.t_tts_fetched_ms - self.t_tts_url_ms

    def emit(self) -> None:
        """Emit a single structured [TURN] log line."""
        self.derive()

        def fmt(v: int) -> str:
            return f"+{v}ms" if v >= 0 else "—"

        audio_ms = self.audio_frames * 80  # each frame is 80ms at 16kHz
        log.info(
            f"[TURN] trigger={self.trigger} outcome={self.outcome} "
            f"total={fmt(self.t_complete_ms)} "
            f"first_frame={fmt(self.t_first_frame_ms)} "
            f"vad_start={fmt(self.t_vad_start_ms)} "
            f"vad_end={fmt(self.t_vad_end_ms)} audio={self.audio_frames}frames/{audio_ms}ms "
            f"stt={fmt(self.t_stt_ms)} text={self.stt_text!r} "
            f"tts_url={fmt(self.t_tts_url_ms)} "
            f"tts_fetch={fmt(self.t_tts_fetched_ms)} tts_bytes={self.tts_bytes} "
            f"playback={fmt(self.t_playback_ms)} "
            f"| segments: ha_think={fmt(self.ha_think_ms)} "
            f"tts_gen={fmt(self.tts_gen_ms)} fetch={fmt(self.fetch_ms)}"
        )


async def record_dropped_turn(device, trigger_label: str, wake_info) -> None:
    """
    Persist a stub record for a turn that never started (no ESPHome server /
    no HA connection). Without this, wakes during an HA outage would vanish
    from the activity history — exactly the "device woke but nothing
    happened" events worth seeing in a trend review.

    Also surfaces the outcome to the turn loop's ring cleanup, the same way a
    completed turn does — the user standing in front of the device is the one
    with no other way to find out. Without it the ring lit and cleared inside
    a few milliseconds, which is the "looked like a glitch" failure ack_anim
    was added for, on a device that will not answer until HA connects.
    """
    wi = wake_info or {}
    turn_record = {
        "ts":             time.time(),
        "trigger":        trigger_label,
        "wake_model":     wi.get("model"),
        "wake_score":     wi.get("score"),
        "wake_threshold": wi.get("threshold"),
        "noise_floor":    wi.get("noise_floor"),
        "outcome":        "no_ha",
        "total_ms":       0,
    }
    await persist_turn(device, turn_record)
    device.turn_history.append(turn_record)
    device.last_turn_outcome = turn_record["outcome"]


async def _save_utterance(device, turn_id: int, turn_record: dict) -> None:
    """
    Write the turn's captured mic audio (if saveUtterances is on) to
    recordings/ and record the filename on the turn row.

    Runs after the insert because the filename is keyed on the rowid. The
    buffer is consumed here — a turn that never streamed audio must not
    inherit the previous turn's recording, and holding ~1MB of speech on
    the Device past its one use has no upside.

    Best-effort throughout: a full disk or a read-only volume costs the
    recording, never the turn.
    """
    pcm = getattr(device, "last_utterance_pcm", None)
    device.last_utterance_pcm = None
    if not pcm:
        return
    try:
        name = await asyncio.get_running_loop().run_in_executor(
            None, em_recordings.save, device.device_id, turn_id, pcm
        )
    except Exception as e:
        log.warning(f"[{device.device_id}] Utterance save failed: {e}")
        return
    if not name:
        return
    turn_record["audio_file"] = name
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, db.set_turn_audio, turn_id, name
        )
    except Exception as e:
        log.warning(f"[{device.device_id}] Utterance link failed: {e}")
        return
    log.info(
        f"[{device.device_id}] Utterance saved: {name} "
        f"({em_recordings.duration_ms(len(pcm)) / 1000:.1f}s)"
    )


async def persist_turn(device, turn_record: dict) -> None:
    """
    Write a completed turn to SQLite (survives controller restarts) and
    remember the rowid on the device so the firmware's asynchronous
    playback_stats report — which lands after the turn has already been
    recorded — can attach its underrun count to this turn. Best-effort:
    a DB hiccup must never take down the voice pipeline.
    """
    loop        = asyncio.get_running_loop()
    played      = (turn_record.get("playback_ms") or -1) >= 0
    pending     = device.pending_playback_stats
    device.pending_playback_stats = None
    if played and pending is not None and loop.time() - pending[0] < 30.0:
        # Device reported this playback's stats before the turn row existed
        # (its buffer drained ahead of our overestimated drain sleep).
        turn_record["playback_periods"] = pending[1]
        turn_record["underruns"]        = pending[2]
        # v7 delivery detail (tuple len 5 from firmware >= v2.9.6; older
        # stashes are 3-tuples and simply carry none of this).
        if len(pending) >= 5:
            pstats = pending[3] or {}
            turn_record["min_depth"]     = pstats.get("minDepth")
            turn_record["prime_wait_ms"] = pstats.get("primeWaitMs")
            turn_record["recv_span_ms"]  = pstats.get("recvSpanMs")
            turn_record["max_gap_ms"]    = pstats.get("maxGapMs")
            turn_record["bytes_recv"]    = pstats.get("bytesRecv")
            if pending[4] is not None and pending[4] >= 0:
                turn_record["delivery_ms"] = pending[4]
            turn_record["send_ms"] = device.playback_send_ms
            turn_record["eq_ms"]   = device.playback_eq_ms
        pending = None
    # On-device shadow comparison (schema v13): when THIS controller woke, did
    # the device's own scoring agree, and how far apart were they? Correlated
    # here rather than at detection time on purpose — the device's crossing
    # report travels over the control plane and can land after the wake it
    # belongs to, and by turn end it has had seconds to arrive.
    #
    # dev_shadow records whether the device was scoring at all, so a NULL score
    # beside a 1 is a genuine miss while a NULL beside a NULL is just absence of
    # data. Only for wake-triggered turns: a button press has no wake instant to
    # compare against, and writing 0 there would invent a miss.
    wake_mono = device.last_wake_mono
    device.last_wake_mono = None
    if wake_mono is not None and str(turn_record.get("trigger", "")).startswith("wakeword"):
        dev_score, dev_delta = device.shadow.match(wake_mono)
        turn_record["dev_shadow"]        = 1 if device.shadow.active else None
        turn_record["dev_wake_score"]    = dev_score
        turn_record["dev_wake_delta_ms"] = dev_delta
        # The bar the DEVICE was using. Without it a non-crossing cannot be
        # judged: this controller may have woken at a lower barge-in threshold,
        # which the device was never asked to match.
        turn_record["dev_threshold"]     = device.shadow.threshold
        if device.shadow.active:
            log.info(
                f"[{device.device_id}] shadow comparison: controller "
                f"{turn_record.get('wake_score')} vs device "
                f"{dev_score if dev_score is not None else 'MISS'}"
                + (f" ({dev_delta:+d}ms)" if dev_delta is not None else "")
            )
        # owwOnDevice="on" inverts the question. The device triggered this
        # turn, so the side that can be missing is OURS: a NULL here is a wake
        # this controller did not hear, which is the direction that argues
        # on-device scoring is worth its ~38% of a core. Only recorded when the
        # device actually drove the turn — on a controller-triggered turn the
        # column does not apply, and writing our own score into it would make
        # every row agree with itself.
        # A privately listening Echo sends this controller nothing to score,
        # so there is no comparison to make and no miss to record.
        if (str(turn_record.get("trigger", "")).startswith("wakeword-dev")
                and not getattr(device, "private_listening", False)):
            ctrl_score, ctrl_delta = device.ctrl_shadow.match(wake_mono)
            turn_record["ctrl_wake_score"]    = ctrl_score
            turn_record["ctrl_wake_delta_ms"] = ctrl_delta
            log.info(
                f"[{device.device_id}] wake comparison: device "
                f"{turn_record.get('wake_score')} vs controller "
                f"{ctrl_score if ctrl_score is not None else 'MISS'}"
                + (f" ({ctrl_delta:+d}ms)" if ctrl_delta is not None else "")
            )

    # Surface the outcome to the turn loop's ring cleanup. Without this
    # every ending looks identical to the user — "I didn't hear you",
    # "HA errored" and "done, nothing to say" all just turn the ring off.
    device.last_turn_outcome = turn_record.get("outcome")
    try:
        turn_id = await loop.run_in_executor(
            None, db.insert_turn, device.device_id, turn_record
        )
        turn_record["turn_id"] = turn_id
        await _save_utterance(device, turn_id, turn_record)
        if played and "underruns" not in turn_record:
            # Playback happened but its stats haven't arrived — leave the
            # rendezvous open for handle_control's playback_stats branch.
            device.last_turn_id = turn_id
    except Exception as e:
        log.warning(f"[{device.device_id}] Failed to persist turn: {e}")
    if pending is not None and pending[2]:
        # Stale/unmatched stash (announcement playback) — keep its underruns
        # in the hourly counters rather than dropping them.
        try:
            await loop.run_in_executor(
                None,
                lambda: db.bump_wake_counters(
                    device.device_id, underruns=pending[2]
                ),
            )
        except Exception:
            pass
