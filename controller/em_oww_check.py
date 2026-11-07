"""
em_oww_check.py — can this file run as a wake word model?
==========================================================

An uploaded .onnx used to be checked for its name and size only, so a file
that could not run was installed and failed later, in the wake listener
(#869). `classify()` is asked before the upload is written: it names the
engine the file belongs to ("oww" or "livekit_v2", em_oww_models) and says
why it cannot run. `problem()` is the openwakeword-only question, unchanged
for its callers and tests.

What is required is what the two consumers do with a classifier, read from
their source rather than from typical models:

  openwakeword 0.6.0 (model.py)  feeds input 0 with the last
      `inputs[0].shape[1]` embeddings and reads `outputs[0].shape[1]` as the
      class count. 1 means one score keyed by the filename stem. Anything
      else means per-class keys ("0", "1", ...), so the stem scores 0.0
      forever. A dimension that is not a number fails the load.
  the firmware (wakeword/ort)    feeds input 0 with [1, 16, 96] and returns
      an error unless output 0 holds exactly one value.
  livekit.wakeword 2.x (model.py, _Stream) feeds input `chunk` with one
      (1, 1280) float frame and every other input as state, and takes
      output 0 as the score and outputs 1: as the state re-injected by the
      name `<input>_out`. The state lives inside the graph, so no other
      engine — not openwakeword, not the Echo's firmware — can run it; the
      controller scores such models itself (see forced_mode in em_shadow).

What can run on neither is refused. A window other than 16 is accepted:
the controller's listener runs it, the Echo's own cannot. A [N, 1, 1]
output is accepted: both sides read its one value
(em_oww_models.scalar_score). A window or class count that is dynamic is
refused although the Echo alone could run it, because openwakeword raises
on it and the controller loads the model for barge-in whichever side
listens.

This proves a model can run. It cannot show the model detects its phrase.

The shape and score rules are pure functions so the suite covers them
without onnxruntime; `classify()` is the only part that loads a model.
"""

from __future__ import annotations

import math

import em_oww_models

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


def livekit_shape_problem(inputs: list[tuple[str, list, str]],
                          outputs: list[tuple[str, list, str]]) -> str | None:
    """
    Why a livekit_v2 model cannot run on the livekit.wakeword client, or None.
    Both lists are [(name, shape, type)] as onnxruntime reports them.

    The rules come from what _Stream does, not from a v2 export in particular:
    it feeds `chunk` as (1, 1280) float, zero-initialises every other input by
    its declared shape, and after each step re-injects outputs[1:] under the
    state inputs' names with "_out" removed. A missing or misnamed state output
    does not fail at export or load — the client raises mid-stream, in the wake
    listener, over a score the user never gets. Checking the pairing here names
    the file instead.
    """
    chunk = next((i for i in inputs if i[0] == "chunk"), None)
    if chunk is None:
        return "it has no streaming audio input named 'chunk'"
    if chunk[2] != _FLOAT or len(chunk[1]) != 2 or chunk[1][-1] != em_oww_models.LIVEKIT_CHUNK:
        return (f"its 'chunk' input is {_fmt(chunk[1])} {chunk[2]} and must be "
                f"[N, {em_oww_models.LIVEKIT_CHUNK}] {_FLOAT}")
    if not outputs:
        return "it has no output"
    if outputs[0][2] != _FLOAT:
        return f"its score output is {outputs[0][2]} and must be {_FLOAT}"
    for i in inputs:
        if i[0] == "chunk":
            continue
        if not all(_is_dim(d) for d in i[1]):
            return (f"its state input '{i[0]}' is {_fmt(i[1])} — the stream "
                    "starts from zeros sized by the declared shape, and "
                    "cannot size this one")
    state_in = [i[0] for i in inputs if i[0] != "chunk"]
    state_out = {o[0][:-len("_out")] for o in outputs[1:] if o[0].endswith("_out")}
    missing = [n for n in state_in if n not in state_out]
    if missing:
        return ("it does not carry its state through: no '" + "_out', '".join(missing)
                + "_out' output for the state input"
                + ("s" if len(missing) > 1 else "") + " " + ", ".join(missing))
    return None


def _is_dim(d) -> bool:
    return isinstance(d, int) and not isinstance(d, bool) and d > 0


def _fmt(shape: list) -> str:
    return "[" + ", ".join("N" if not isinstance(d, int) else str(d)
                           for d in shape) + "]"


def problem(data: bytes) -> str | None:
    """
    Why these bytes cannot run as an openwakeword model, or None if they can
    (or if onnxruntime is not importable, where nothing can be said).
    Loads the model and runs it once on silence: call it off the event loop.
    """
    kind, why = classify(data)
    if kind is None and why is None:
        return None      # no onnxruntime: nothing can be said, as before
    return why


def classify(data: bytes) -> tuple[str | None, str | None]:
    """
    (kind, why) — which engine these bytes run on, and why they cannot run.

    kind is None when it cannot be said: the bytes are not a loadable model,
    or the signature matches neither engine (why then names the openwakeword
    shape rule, as uploads were refused with since #869), or onnxruntime is
    not importable — where why is None too, the same nothing-to-say answer
    problem() has always given for that environment. For KIND_OWW the `why`
    is exactly problem()'s, to the string: the upload endpoint's error shape
    does not move.

    Loads the model and runs its first frames on silence: call it off the
    event loop.
    """
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError:
        return None, None
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
        return None, f"it is not an ONNX model onnxruntime can load ({_first_line(e)})"

    in_named = [(i.name, list(i.shape), i.type) for i in sess.get_inputs()]
    out_named = [(o.name, list(o.shape), o.type) for o in sess.get_outputs()]
    kind = em_oww_models.classify_signature(in_named,
                                            sess.get_modelmeta().custom_metadata_map)

    if kind == em_oww_models.KIND_LIVEKIT:
        why = livekit_shape_problem(in_named, out_named)
        if why:
            return kind, why
        return kind, _livekit_run(sess, np, in_named)

    inputs = [(shape, typ) for _n, shape, typ in in_named]
    outputs = [(shape, typ) for _n, shape, typ in out_named]
    why = shape_problem(inputs, outputs)
    if why:
        return (kind, why) if kind else (None, why)
    feats = np.zeros((1, window(inputs), FEAT_DIM), dtype=np.float32)
    try:
        out = sess.run([sess.get_outputs()[0].name],
                       {sess.get_inputs()[0].name: feats})[0]
    except Exception as e:
        return kind, f"it failed on its first frame ({_first_line(e)})"
    return kind, score_problem([float(v) for v in np.ravel(out)])


def _livekit_run(sess, np, in_named) -> str | None:
    """
    The first two steps of the stream, the way _Stream takes them — zero state
    (the client's own cold start) and then the model's outputs fed back by
    name — so a signature that only fails mid-stream fails at upload instead.
    """
    state = {i[0]: np.zeros(i[1], np.int64 if "int64" in i[2] else np.float32)
             for i in in_named if i[0] != "chunk"}
    names = [o.name.removesuffix("_out") for o in sess.get_outputs()[1:]]
    try:
        for _ in range(2):
            out = sess.run(None, {"chunk": np.zeros(
                (1, em_oww_models.LIVEKIT_CHUNK), np.float32), **state})
            why = score_problem([float(v) for v in np.asarray(out[0]).reshape(-1)])
            if why:
                return why
            state = dict(zip(names, out[1:]))
    except Exception as e:
        return f"it failed on its first frame ({_first_line(e)})"
    return None


def _first_line(e: Exception) -> str:
    text = str(e).strip().splitlines()
    return (text[0] if text else type(e).__name__)[:160]
