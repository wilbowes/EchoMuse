"""
Saving config: pushing a device's effective config to its live connection
and the controller-side copies of it, refusing a write that would silently
drop keys, and the emOS console password and idle timeout, which are stored
and shown differently from every other key. Shared by the device and fleet
config routes.
"""

from __future__ import annotations

import logging

import em_config_types
import em_console_pw
import em_esphome
import em_oww_sync
import em_scenes
import em_shadow
import em_tasks
import em_turnclock

log = logging.getLogger("echomuse.api")


async def apply_live_config(device_id: str, live, effective: dict) -> None:
    """
    Push an effective config to a connected device and refresh the
    controller-side mirrors of it.

    Extracted because the per-device and fleet endpoints both did this
    inline, and a mirror added to one but not the other is a bug that reads
    as working — the same shape as the v7 stats-relay miss (PR #23). Take
    the EFFECTIVE config, never a request body: with per-section scoping a
    body is partial by design, and a device must always be sent the whole
    resolved picture.

    One key is held back: a NEW `owwModel` is not sent to a device that scores
    locally until the classifier is actually on it — see em_oww_sync.hold_back_model.
    """
    effective = em_config_types.well_typed(device_id, effective)
    effective, pending_model = em_oww_sync.hold_back_model(live, effective)
    await live.send_control({"type": "config", **effective})
    if "owwThreshold" in effective:
        live.oww_threshold = float(effective["owwThreshold"])
    if "owwModel" in effective:
        live.oww_model = effective["owwModel"]
        # Refresh HA's wake-word dropdown.
        await em_esphome.update_oww_model(device_id, effective["owwModel"])
    if pending_model:
        # The device is still on its previous wake word, still scoring
        # locally, still answering. Install, then switch.
        em_tasks.spawn(em_oww_sync.install_then_switch(device_id, pending_model))
    if "owwSpeexNs" in effective:
        live.oww_speex_ns = bool(effective["owwSpeexNs"])
    if "nsAsr" in effective:
        live.ns_asr = bool(effective["nsAsr"])
    if "saveUtterances" in effective:
        live.save_utterances = bool(effective["saveUtterances"])
    if "wakeClipCapture" in effective:
        live.wake_clip_capture = bool(effective["wakeClipCapture"])
        if not live.wake_clip_capture:
            live.wake_capture.reset()
    if "wakeClipMinScore" in effective:
        try:
            live.wake_clip_min_score = max(0.05, min(0.95, float(effective["wakeClipMinScore"])))
        except (TypeError, ValueError):
            live.wake_clip_min_score = 0.20
    if "streamReply" in effective:
        live.stream_reply = bool(effective["streamReply"])
    if "bargeInEnabled" in effective:
        live.barge_in_enabled = bool(effective["bargeInEnabled"])
    if "bargeInThreshold" in effective:
        live.barge_threshold = float(effective["bargeInThreshold"])
    if "buttonSingleTapEvent" in effective:
        live.button_single_tap_event = bool(effective["buttonSingleTapEvent"])
    if "buttonMultiTapMs" in effective:
        live.button_multi_tap_ms = int(effective["buttonMultiTapMs"])
    if "wakeArbitrationMs" in effective:
        live.wake_arb_ms = int(effective["wakeArbitrationMs"])
    # Consumed controller-side ONLY on the controller-detected wake path
    # (#120): a device that detects its own wake plays the cue itself and
    # never consults this. Mirrored anyway, because the path that does need
    # it is the one where the setting would otherwise silently do nothing.
    if "wakeSound" in effective:
        live.wake_sound = bool(effective["wakeSound"])
    em_turnclock.apply_listen(live, effective)
    if "micGainDb" in effective:
        live.mic_gain_db = float(effective["micGainDb"])
    # What a wake's level reading still contains; see em_arbiter.pick.
    if "adcMicpga" in effective:
        live.adc_gains = (int(effective["adcMicpga"]), live.adc_gains[1])
    if "adcDigitalGain" in effective:
        live.adc_gains = (live.adc_gains[0], int(effective["adcDigitalGain"]))
    if "owwOnDevice" in effective:
        # Resolved against the CAPABILITY, not taken at face value: "on"
        # against firmware that cannot trigger would stop this controller
        # acting on its own detections while waiting for wakes the device has
        # no code to send, leaving it deaf. em_shadow.effective_mode degrades
        # that to shadow.
        new_oww_on_device = em_shadow.effective_mode(
            effective["owwOnDevice"], live.oww_trigger_capable,
        )
        if new_oww_on_device != live.oww_on_device:
            # #696 review: a buffered pre-roll from the old mode must not
            # carry into the new one.
            live.wake_capture.reset()
        live.oww_on_device = new_oww_on_device
    if "eqBands" in effective:
        live.eq_bands = effective["eqBands"]
    if "eqLoudness" in effective:
        live.eq_loudness = bool(effective["eqLoudness"])
    # The output chain is consumed HERE, not on the device — it ignores these
    # five keys entirely — so this mirror is the only thing that carries them.
    # Missing it meant a push wrote the database, sent JSON the device threw
    # away, and changed nothing audible until the device happened to
    # reconnect. Exactly the shape this function's docstring warns about, and
    # it cost a whole listening test on 2026-08-19: every setting appeared to
    # do nothing, because every setting WAS doing nothing.
    if "limiterEnabled" in effective:
        live.limiter_enabled = bool(effective["limiterEnabled"])
    if "limiterThreshold" in effective:
        live.limiter_threshold = float(effective["limiterThreshold"])
    if "limiterRelease" in effective:
        live.limiter_release = float(effective["limiterRelease"])
    if "bassGuardEnabled" in effective:
        live.bass_guard_enabled = bool(effective["bassGuardEnabled"])
    if "bassGuardDb" in effective:
        live.bass_guard_db = float(effective["bassGuardDb"])
    live.led_scene = em_scenes.resolve(effective)
    # #263: keep the device's cached listening animation in step when the
    # scene changes live, same push as at registration. Without it the ring
    # lit locally in the OLD scene's colours until the next reconnect.
    if live.led_anim_capable and live.led_scene.get("listening_anim"):
        try:
            await live.send_control(
                {"type": "config",
                 "listeningAnim": live.led_scene["listening_anim"]})
        except Exception:
            pass  # device offline — next connect re-sends it


# The console password record never leaves the controller. Hashing before
# storage is pointless if the result is then handed to every client that asks
# for the config, so reads see a sentinel and writes send it back untouched —
# see em_console_pw.for_display / resolve_write.
_CONSOLE_PW_KEY = "consolePassword"


def redact_console_pw(config: dict) -> dict:
    if not config or _CONSOLE_PW_KEY not in config:
        return config
    out = dict(config)
    out[_CONSOLE_PW_KEY] = em_console_pw.for_display(out[_CONSOLE_PW_KEY])
    return out


def resolve_console_pw(incoming: dict, stored: dict) -> None:
    """Turn whatever a client sent into the record to store, in place."""
    if _CONSOLE_PW_KEY not in incoming:
        return
    incoming[_CONSOLE_PW_KEY] = em_console_pw.resolve_write(
        incoming[_CONSOLE_PW_KEY], (stored or {}).get(_CONSOLE_PW_KEY)
    )


# The console idle timeout, in minutes: 0 for none, otherwise 1-90.
_CONSOLE_TMOUT_KEY = "consoleTimeoutMin"


_CONSOLE_TMOUT_MAX = 90


def validate_console_timeout(config: dict) -> str | None:
    """
    Return an error message when the timeout is out of range, else None.

    REFUSED rather than clamped, deliberately. A value of 600 is somebody who
    meant seconds, and silently giving them ten minutes is a console that logs
    them out all day from a setting that looked accepted. The device refuses
    it too — the controller validating first means a bad value reaching the
    firmware is a bug rather than a user, but neither end assumes the other is
    the careful one.

    Absence is fine: it means the body did not mention the key.
    """
    if _CONSOLE_TMOUT_KEY not in config:
        return None
    v = config[_CONSOLE_TMOUT_KEY]
    if isinstance(v, bool) or not isinstance(v, int):
        return (f"{_CONSOLE_TMOUT_KEY} must be a whole number of minutes, "
                f"got {v!r}")
    if v < 0 or v > _CONSOLE_TMOUT_MAX:
        return (f"{_CONSOLE_TMOUT_KEY} must be 0 (no timeout) or 1-"
                f"{_CONSOLE_TMOUT_MAX} minutes, got {v}")
    return None


def dropped_keys(incoming: dict, stored: dict) -> list[str]:
    """
    Keys present in the stored config that the incoming body would delete.

    Config POSTs REPLACE the stored dict — they do not merge. That is fine
    for the dashboard, which always submits the complete config, and a trap
    for anything that submits a partial one: on 2026-07-20 a POST carrying a
    single key silently reset all 26 fleet settings to defaults, taking the
    wake model from hey_mycroft back to hey_jarvis and dropping owwThreshold
    0.5 -> 0.3, which surfaced as devices false-waking on ordinary
    conversation. Callers that genuinely intend a destructive write pass
    replace=true; everything else is refused before anything is persisted.
    """
    return sorted(set(stored) - set(incoming))
