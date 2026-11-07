"""
em_wake_engine.py — what actually scores a wake word on the controller
======================================================================

`owwModel` names a model, and two engines can run one:

- openwakeword's `Model` (em_oww_models.model_class, lazy-imported there) for
  stock names and for custom openwakeword-format .onnx. Constructed as
  `Model(wakeword_models=[name], enable_speex_noise_suppression=speex)`.
- LiveKit WakeWord v2 (`livekit.wakeword.WakeWordModel`) for a livekit_v2
  file — one whose ONNX graph streams its own state (see
  em_oww_check.livekit_shape_problem). Neither openwakeword nor the Echo's
  firmware can load that format, so these models are scored here and only
  here: selecting one forces owwOnDevice off (em_shadow.forced_mode).

Both engines take the same int16 chunks as `predict` and answer a dict
keyed per model, and both have `reset()`, so the wake listener and the
barge watcher score either with their existing lines unchanged. The key
agreement is not luck: openwakeword keys custom models by the filename
stem (em_oww_models.prediction_key) and WakeWordModel names a loaded model
by the file stem too — for a livekit model read from `oww_models/`, both
are the stem of the same path.

`build` is the factory: it reads the kind from the registry and returns
the engine. It may load the model file (kind classification) and imports
the engine package on first use — the ~120 MB / ~2 s openwakeword import
and the livekit one — so like model_class() it belongs to the executor
threads that build models, never the event loop. em_controller acquires
its models with the same kind check rather than through `build`: the
openwakeword branch there wraps the construction in the raw-audio-buffer
trim (_trim_oww_raw_buffer), which lives where the wake loop lives, and
the reuse guard pins that construction's shape.
"""

from __future__ import annotations

import em_oww_models


def build(name: str, speex: bool = False):
    """The wake engine for an `owwModel` value. Call off the event loop."""
    if em_oww_models.is_livekit_model(name):
        return build_livekit(name)
    Model = em_oww_models.model_class()
    return Model(wakeword_models=[name], enable_speex_noise_suppression=speex)


def build_livekit(path: str):
    """
    A LiveKit WakeWord v2 model, imported on first use.

    `predict(frame)` takes consecutive frames of 1280 int16 samples at
    16 kHz and returns {stem: highest score across the steps that frame
    completed} — the streaming loop the client's own examples run. Building
    it opens an ONNX session and warms it with 2 s of silence, ~150 ms for
    the 7.4 MB v2 model (measured), which is why it lands in an executor
    thread like the openwakeword build beside it. The v2 package imports
    only numpy and onnxruntime, and importing this module never touches it.
    """
    from livekit.wakeword import WakeWordModel
    return WakeWordModel(models=[path])
