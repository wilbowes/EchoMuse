"""
The barge-in watcher: scores the Echo's microphone while it plays a reply
and cuts the reply when the wake word is said over it. Whether a score counts
is em_barge's pure decision; this is the loop around it.

Moved out of em_controller (2026-11) with no change to the body.
"""

import asyncio
import logging

import numpy as np

import em_barge
import em_db as db
import em_dbwriter
import em_device
import em_esphome as esphome
import em_listen
import em_oww_models
import em_oww_warmup
import em_state
import em_wake_claim
import em_wake_model
import em_wakelevel
import em_wakeword
import em_wire

log = logging.getLogger("echomuse")


async def barge_watcher(device: em_device.Device, playback_started: asyncio.Event):
    """
    Wake-word watcher spanning the thinking AND playback phases (barge-in,
    §3.2). Started at STT_VAD_END (on_thinking); before that the user's own
    command is streaming and a wake word in it is just speech.

    With barge-in enabled the mic keeps streaming through the whole turn
    (the device's AEC subtracts its own speaker output during playback) and
    oww_paused routes frames to voice_queue — which nothing else reads
    after STT ends, so this watcher drains and scores it with a dedicated
    openwakeword instance (the main wake listener task is blocked awaiting
    the turn).

    The threshold is phase-dependent because the acoustics are: during
    playback the speaker is ~25dB louder than the person at the mic, so
    speech-over-TTS scores are depressed and barge_threshold sits well
    below the wake threshold (~0.05–0.10). During thinking nothing is
    playing — scores are normal, and using the low barge threshold there
    would fire on random speech — so a single frame at the normal wake
    threshold fires, and nothing below it does.

    There was a second, lower tier until #337, added for a genuine barge
    that plateaued at 0.240/0.242 against a threshold of 0.50 (2026-07-12)
    and was missed. That reading came off a COLD model, which is the fault
    `em_oww_warmup` was later written to fix; the compensation outlived its
    cause and became pure false-positive surface, cancelling a real request
    at 0.206/0.238 on 2026-08-25. See em_barge.decide.

    On detection: set barge_detected + cancel_event. During playback that
    aborts stream_speaker and the drain sleep, plus a device speaker_flush
    so the interruption is audible immediately (not after ~1.4s of queued
    TTS). During thinking there's no audio to flush — instead the in-flight
    HA pipeline is cancelled (local-only; any late HA result is discarded).
    """
    loop = asyncio.get_event_loop()
    # Reused across reconnects via the module cache (#512): a new Device is
    # created per connection, so caching on the Device rebuilt this every time.
    name = device.oww_model
    model = await em_wake_model.acquire_barge_model(device, name)
    # name is the raw owwModel value; scoring needs the openwakeword prediction
    # key (path → stem).
    barge_pred_key = em_oww_models.prediction_key(name)
    await em_wake_model.reset_model(model)
    # reset() seeds the classifier's window with embeddings of random noise,
    # so the first FEATURE_WINDOW chunks score that noise as much as the room.
    # This watcher is where that hurt most: it reset and scored immediately,
    # and twice cancelled a turn the user was waiting on, at 0.867 and 0.700
    # within 10 chunks of starting. See em_oww_warmup.
    warmup = em_oww_warmup.WarmupGate()

    # Drop anything queued before the watcher started (command tail,
    # silence) — only fresh audio should be scored.
    while not device.voice_queue.empty():
        try:
            device.voice_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    # Playback phase: bargeInThreshold is used as-is — deliberately NOT
    # floored at the wake threshold. The max() clamp guarded against
    # residual echo waking the device before AEC worked; measured with
    # working AEC (2026-07-08), self-echo peaks at 0.004 converged / 0.055
    # worst-case-unconverged, while real speech over TTS scores 0.118+ —
    # the echo is 25dB louder than the speaker at the mic, so
    # speech-over-TTS scores are inherently depressed and a sub-wake
    # threshold (~0.10) is both safe and necessary. Thinking phase uses the
    # normal wake threshold (see docstring).
    threshold = device.barge_threshold  # refined per-frame by phase below
    prev_score = 0.0  # previous frame's score — both phases need two
    prev_heard = None  # when that frame was captured, to date a two-frame barge
    buf = bytearray()
    # Observability: the watcher used to log only on detection, which made a
    # failed barge-in attempt indistinguishable from "no frames arrived at
    # all" (mic not streaming) or "frames arrived but scored ~0" (AEC residual
    # burying the speech). Track both and always report on exit.
    peak   = 0.0
    frames = 0
    # Frame RMS (0.0–1.0) discriminates the failure modes peak alone can't:
    # rms >> noise floor means echo is reaching the watcher raw (AEC off or
    # ineffective — delay mismatch / clipped-nonlinear echo); rms ≈ floor
    # with a low peak means AEC is eating the user's speech along with the
    # echo (over-suppression / divergence during double-talk).
    rms_sum = 0.0
    rms_max = 0.0
    levels = em_wakelevel.LevelRing()
    try:
        while True:
            payload = await device.voice_queue.get()
            if payload is None or isinstance(payload, str):
                buf.clear()
                prev_score = 0.0  # sentinel = stream discontinuity; frames
                # across it are not consecutive for either two-frame rule
                prev_heard = None
                levels.clear()
                continue
            buf.extend(payload)
            heard = em_listen.captured(payload, loop.time())
            while len(buf) >= em_wire.CHUNK_BYTES:
                frame = bytes(buf[:em_wire.CHUNK_BYTES])
                del buf[:em_wire.CHUNK_BYTES]
                samples = np.frombuffer(frame, dtype=np.int16)
                rms = float(np.sqrt(np.mean((samples.astype(np.float64) / 32768.0) ** 2)))
                rms_sum += rms
                rms_max  = max(rms_max, rms)
                levels.push(rms)
                prediction = await loop.run_in_executor(None, model.predict, samples)
                score = em_oww_models.scalar_score(prediction.get(barge_pred_key, 0.0))
                frames += 1
                trusted = warmup.feed()
                in_playback = playback_started.is_set()
                # em_barge owns both phases. Extracted because this shipped
                # untested and wrong: the playback branch fired on ONE frame
                # at a bar ten times lower than the wake threshold, while
                # scoring the device's own speech, and cut long responses off
                # mid-sentence (measured 2026-08-20).
                threshold = (device.barge_threshold if in_playback
                             else device.oww_threshold)
                fired, fire_note = em_barge.decide(
                    score=score,
                    prev_score=prev_score,
                    in_playback=in_playback,
                    barge_threshold=device.barge_threshold,
                    wake_threshold=device.oww_threshold,
                )
                if fired and not trusted:
                    log.info(
                        f"[{device.device_id}] Barge watcher: {fire_note} "
                        f"ignored — openwakeword warm-up, "
                        f"{warmup.progress()} chunks since reset"
                    )
                    fired = False
                if fired and not em_wakeword.wake_allowed(
                        mic_muted=device.muted,
                        enabled=device.wake_word_enabled):
                    # The stream is up for a turn HA or the button started.
                    # An Echo detecting its own wake word drops this crossing
                    # itself; this is the same rule for the ones scored here.
                    log.info(f"[{device.device_id}] barge {score:.3f} suppressed — "
                             f"{'muted' if device.muted else 'wake word off'}")
                    fired = False
                # A playback barge fires on the second of two frames; the
                # utterance was heard at the first.
                fired_heard = (prev_heard if in_playback and prev_heard is not None
                               else heard)
                prev_score = score
                prev_heard = heard
                if score > peak:
                    peak = score
                    if score >= 0.1:
                        log.info(
                            f"[{device.device_id}] Barge watcher: score {score:.3f} "
                            f"(threshold {threshold:.2f})"
                        )
                if fired:
                    phase = "playback" if in_playback else "thinking"
                    log.info(
                        f"[{device.device_id}] Barge-in: wake word during {phase} "
                        f"({fire_note}) — cancelling turn"
                    )
                    em_dbwriter.submit(db.log_device,
                        device.device_id, "info", "device",
                        f"Barge-in during {phase} (score={score:.3f})"
                    )
                    device.barge_detected = True
                    device.barge_decided.clear()

                    # Arbitrate, exactly as a wake crossing does. Until
                    # 2026-09-02 this path claimed nothing, so on a fleet with
                    # two Echoes in earshot a barge-in produced TWO answers:
                    # this device started its interrupting turn while an idle
                    # neighbour heard the same utterance on its ordinary wake
                    # listener and claimed an unopposed arbiter. Reported by
                    # Wil, and it is the exact failure em_arbiter exists to
                    # prevent — including the self-feeding loop where each
                    # device transcribes the other's TTS as a follow-up.
                    #
                    # The original wake's claim cannot cover this: claim() is
                    # bounded by window_s (300-700ms), not held until
                    # release(), so it expired seconds ago.
                    #
                    # Same ORDER as the wake path — can_serve_turn before the
                    # claim, so a device that cannot finish a turn never takes
                    # one. HA can drop mid-turn, which is exactly when a user
                    # talks over an answer that stopped making sense.
                    #
                    # Known asymmetry, deliberately not corrected here: this
                    # device is scoring through its own playback, ~25dB down,
                    # so it is handicapped against an idle neighbour and will
                    # sometimes lose an utterance it was nearer to. The
                    # consequence is that the neighbour answers and this
                    # device only stops talking — one responder either way,
                    # which is the bug being fixed. Giving the barged device
                    # precedence is a SECOND rule that can disagree with the
                    # first, and would need a way to revoke a claim a
                    # neighbour has already started a turn on.
                    if in_playback:  # before the claim, as in _private_barge
                        esphome.note_barged(device.device_id)
                        await device.send_control({"type": "speaker_flush"})
                    serves = esphome.can_serve_turn(device.device_id)
                    won_by = device.device_id
                    if serves and device.wake_arb_ms > 0 and len(em_state.devices) > 1:
                        # Capture time already carries the link's least
                        # delay; its smoothed RTT would count it twice.
                        won_by = await em_wake_claim.claim_wake(
                            device, fired_heard, levels.measure(device.mic_gain_db),
                            over_playback=True)
                    verdict = em_barge.cede(
                        serves=serves, won_by=won_by,
                        device_id=device.device_id, score=score,
                    )
                    device.barge_ceded = verdict.ceded
                    if device.barge_ceded:
                        log.info(
                            f"[{device.device_id}] Barge-in ceded to "
                            f"{won_by if serves else 'nothing — no HA'} "
                            f"(score={score:.3f}) — stopping playback, not "
                            f"taking the turn"
                        )
                        em_dbwriter.submit(db.log_device,
                            device.device_id, "info", "controller",
                            "Barge-in ceded to another device (arbitration)"
                            if serves else
                            "Barge-in heard but no HA connection",
                        )

                    # Wake detail for the interrupting turn's persistent
                    # record — popped when the turn loop re-enters
                    # trigger_voice_turn with trigger "barge-in". Set BEFORE
                    # the no_ha branch below, which carries this one off
                    # instead.
                    device.last_wake = {
                        "model":       barge_pred_key,
                        "score":       round(float(score), 4),
                        "threshold":   float(threshold),
                        "noise_floor": round(device.noise_floor, 5),
                    }
                    if verdict.no_ha:
                        # The same stand-down the wake listener gets for the
                        # same reason (#417), for the same two reasons: a barge
                        # with no trace is indistinguishable from a device that
                        # heard nothing, so an HA outage stays invisible in the
                        # activity history; and the cue reports the DEVICE's
                        # state rather than how the turn ended.
                        #
                        # Only CARRIED here. The turn loop's ceded branch is
                        # where it is recorded, because this block runs before
                        # anything below can unwind the turn: the interrupted
                        # turn's own em_turn_record.persist_turn then overwrites the outcome
                        # with "barged", and _leds_turn_end — which
                        # cleanup_esphome ends on, and which owns the ring —
                        # reads that. Recording here meant the row was right
                        # and the cue never fired.
                        #
                        # So: barge_detected stays SET (the loop's branch
                        # needs it, and needs barge_ceded and cancel_event to
                        # survive to it), and the reason travels beside the wake
                        # detail for the branch to consume.
                        device.barge_no_ha = (verdict.trigger_label,
                                              device.last_wake)
                        device.last_wake = None
                    device.cancel_event.set()
                    device.barge_decided.set()
                    if in_playback:
                        # HA's run is normally over by now (RUN_END follows
                        # TTS_END, and we only reach playback at TTS_END), but
                        # a barge in the first milliseconds of audio can beat
                        # it. Serialise anyway — the interrupting turn is the
                        # thing that pays if we lose that race.
                        esphome.abort_ha_run(device.device_id)
                    else:
                        # Nothing is playing — HA is mid-pipeline, and an
                        # interrupting turn is about to start on the same
                        # connection. The protocol carries no run id, so the
                        # old run MUST be aborted upstream first or its tail
                        # events land on the new turn and kill it
                        # (pipeline_refused, 5 of 5 attempts, 2026-08-17).
                        esphome.cancel_voice_turn(
                            device.device_id, abort_ha=True, reason="barged")
                    # Wake sound for a barge that won, after the flush; a
                    # private Echo plays its own on the session's listen_ack.
                    if not device.barge_ceded and getattr(device, "wake_sound", False)\
                            and device.wake_cue_capable:
                        await device.play_cue("wake")
                    return
    finally:
        device.barge_decided.set()
        rms_mean = rms_sum / frames if frames else 0.0
        log.info(
            f"[{device.device_id}] Barge watcher done: {frames} frames "
            f"({frames * 80}ms) scored, peak={peak:.3f}, threshold={threshold:.2f}, "
            f"rms mean={rms_mean:.4f} max={rms_max:.4f} "
            f"(device noise floor {getattr(device, 'noise_floor', 0.0):.4f})"
        )
