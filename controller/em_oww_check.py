"""
em_oww_check.py — can this file run as a wake word model?
==========================================================

An uploaded .onnx used to be checked for its name and size only, so a file
that could not run was installed and failed later, in the wake listener
(#869). `problem()` is asked before the upload is written.

What is required is what the two consumers do with a classifier, read from
their source rather than from typical models:

  openwakeword 0.6.0 (model.py)  feeds input 0 with the last
      `inputs[0].shape[1]` embeddings and reads `outputs[0].shape[1]` as the
      class count. 1 means one score keyed by the filename stem. Anything
      else means per-class keys ("0", "1", ...), so the stem scores 0.0
      forever. A dimension that is not a number fails the load.
  the firmware (wakeword/ort)    feeds input 0 with [1, 16, 96] and returns
      an error unless output 0 holds exactly one value.

What can run on neither is refused. A window other than 16 is accepted:
the controller's listener runs it, the Echo's own cannot. A [N, 1, 1]
output is accepted: both sides read its one value
(em_oww_models.scalar_score). A window or class count that is dynamic is
refused although the Echo alone could run it, because openwakeword raises
on it and the controller loads the model for barge-in whichever side
listens.

This proves a model can run. It cannot show the model detects its phrase.

The shape and score rules are pure functions so the suite covers them
without onnxruntime; `problem()` is the only part that loads a model.
"""

from __future__ import annotations

import math

FEAT_DIM = 96        # embedding width, fixed by the shared feature models
DEVICE_WINDOW = 16   # embeddings per score on the Echo (wakeword.FeatWindow)

_FLOAT = "tensor(float)"


def shape_problem(inputs: list[tuple[list, str]],
                  outputs: list[tuple[list, str]]) -> str | None:
    """
    Why a model with these inputs and outputs cannot run, or None.
    Each is [(shape, type)] as onnxruntime reports them: a dimension is an
    int, or a str/None when it is dynamic.
    """
    if len(inputs) != 1:
        return f"it has {len(inputs)} inputs and must have one"
    if not outputs:
        return "it has no output"
    shape, kind = inputs[0]
    if kind != _FLOAT:
        return f"its input is {kind} and must be {_FLOAT}"
    if (len(shape) != 3 or not _is_dim(shape[1]) or shape[2] != FEAT_DIM):
        return (f"its input is {_fmt(shape)} and must be "
                f"[N, {DEVICE_WINDOW}, {FEAT_DIM}]")
    shape, kind = outputs[0]
    if kind != _FLOAT:
        return f"its output is {kind} and must be {_FLOAT}"
    if len(shape) < 2 or shape[1] != 1 or any(d != 1 for d in shape[2:]):
        return f"its output is {_fmt(shape)} and must be one score, [N, 1]"
    return None


def score_problem(values: list[float]) -> str | None:
    """Why this output of one run is not a score, or None."""
    if len(values) != 1:
        return f"it gave {len(values)} values for one frame and must give one"
    v = values[0]
    if not math.isfinite(v) or not 0.0 <= v <= 1.0:
        return f"its score on silence is {v:g} and must be between 0 and 1"
    return None


def window(inputs: list[tuple[list, str]]) -> int:
    """Embeddings per score. Call only after shape_problem passed."""
    return inputs[0][0][1]


def _is_dim(d) -> bool:
    return isinstance(d, int) and not isinstance(d, bool) and d > 0


def _fmt(shape: list) -> str:
    return "[" + ", ".join("N" if not isinstance(d, int) else str(d)
                           for d in shape) + "]"


def problem(data: bytes) -> str | None:
    """
    Why these bytes cannot run as a wake word model, or None if they can
    (or if onnxruntime is not importable, where nothing can be said).
    Loads the model and runs it once on silence: call it off the event loop.
    """
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError:
        return None
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    opts.log_severity_level = 4
    try:
        # bytes(): aiohttp hands the upload over as a bytearray, which
        # onnxruntime refuses as "Unable to load from type".
        sess = ort.InferenceSession(bytes(data), sess_options=opts,
                                    providers=["CPUExecutionProvider"])
    except Exception as e:
        return f"it is not an ONNX model onnxruntime can load ({_first_line(e)})"
    inputs = [(list(i.shape), i.type) for i in sess.get_inputs()]
    outputs = [(list(o.shape), o.type) for o in sess.get_outputs()]
    why = shape_problem(inputs, outputs)
    if why:
        return why
    feats = np.zeros((1, window(inputs), FEAT_DIM), dtype=np.float32)
    try:
        out = sess.run([sess.get_outputs()[0].name],
                       {sess.get_inputs()[0].name: feats})[0]
    except Exception as e:
        return f"it failed on its first frame ({_first_line(e)})"
    return score_problem([float(v) for v in np.ravel(out)])


def _first_line(e: Exception) -> str:
    text = str(e).strip().splitlines()
    return (text[0] if text else type(e).__name__)[:160]
