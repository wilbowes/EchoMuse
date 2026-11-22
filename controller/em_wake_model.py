"""
The controller's openWakeWord models, one wake and one barge model per Echo:
built or reused from the per-device cache (#512), reset off the event loop,
and with openWakeWord's 10 s raw-audio buffer trimmed to what scoring reads.

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import collections
import logging

import em_device
import em_oww_models
import em_state

log = logging.getLogger("echomuse")


def reset_model(model) -> "asyncio.Future":
    """Reset an openwakeword model off the event loop; await the result before
    the model scores again.

    reset() re-seeds the feature window from embeddings of 4s of random noise:
    58ms on a desktop against 1.6ms to score a frame, and ~400ms on the HA
    host. Called on the loop it stalled every Echo's speaker audio, LED frames
    and control messages on every controller-scored wake (measured
    2026-09-22: `event loop stalled 360ms`, and a neighbour's wake read 368ms
    late). Scoring already ran in the executor for less than a thirtieth of
    that cost."""
    return asyncio.get_event_loop().run_in_executor(None, model.reset)


# openWakeWord's AudioFeatures keeps ~10s of raw audio (160k Python ints) and
# converts the whole deque to a list on every predict() to take the last
# ~1.7k samples. Nothing else reads it, and trimming it changes no score
# (tools/trim_check.py: 1200 frames, 0 differing, max delta 0). This is the
# idle-CPU hot path the wake loop hits 12.5 times a second per device.
_OWW_RAW_BUFFER_KEEP = 8 * 1280 + 480  # 8 chunks + the mel tail


def trim_raw_buffer(model):
    try:
        pre = model.preprocessor
        buf = pre.raw_data_buffer
        if buf.maxlen != _OWW_RAW_BUFFER_KEEP:
            pre.raw_data_buffer = collections.deque(buf, maxlen=_OWW_RAW_BUFFER_KEEP)
    except Exception as exc:
        # Never fail wake-model acquisition over an optimisation, but say when
        # the buffer is gone (a renamed one) instead of silently paying the copy.
        log.warning("wake model raw-audio buffer not trimmed (%s) — every "
                    "predict() keeps copying the full 10s buffer", exc)
    return model


async def acquire_wake_model(device: "em_device.Device", name: str, speex: bool):
    """Return the device's wake model, reusing the cached one when the wake word
    and speex setting are unchanged, else building a fresh one and replacing any
    previous entry for this device (#512).

    A cache hit is reset() so a reused model is indistinguishable from a fresh
    build; a miss is a freshly constructed model (already noise-seeded), so it
    is NOT reset. Either way the caller's WarmupGate stays un-warmed, which is
    correct for both."""
    loop = asyncio.get_event_loop()
    cached = em_state.oww_models.get(device.device_id)
    if cached is not None and cached[0] == name and cached[1] == speex:
        model = cached[2]
        await reset_model(model)
        trim_raw_buffer(model)
        return model
    model = await loop.run_in_executor(
        None,
        lambda: trim_raw_buffer(em_oww_models.model_class()(
            wakeword_models=[name],
            enable_speex_noise_suppression=speex,
        )),
    )
    em_state.oww_models[device.device_id] = (name, speex, model)
    em_state.live_oww_models.add(model)
    return model


async def acquire_barge_model(device: "em_device.Device", name: str):
    """Return the device's barge-in model, reusing the cached one when the wake
    word is unchanged, else building a fresh one and replacing any previous
    entry (#512). The barge watcher resets the model itself after acquiring it,
    so no reset here. The barge watcher is gated by voice_lock (one turn per
    device), so two barge watchers never score one model concurrently."""
    cached = em_state.oww_barge_models.get(device.device_id)
    if cached is not None and cached[0] == name:
        return cached[1]
    model = await asyncio.get_event_loop().run_in_executor(
        None, lambda: trim_raw_buffer(em_oww_models.model_class()(wakeword_models=[name]))
    )
    em_state.oww_barge_models[device.device_id] = (name, model)
    em_state.live_oww_models.add(model)
    return model
