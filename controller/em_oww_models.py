"""
em_oww_models.py — custom openWakeWord model discovery
=======================================================

Custom wake-word models (trained by oww_forge, or any openWakeWord-
compatible .onnx) live in `oww_models/` next to the SQLite database —
inside the persisted data volume in Docker, so models survive image
upgrades. The controller passes `owwModel` straight to
`OWWModel(wakeword_models=[...])`, which accepts file paths as well as
stock model names, so "installing" a model is just: file lands in the
dir, config points at its absolute path.

This module is pure path/filesystem logic (no aiohttp, no db import) so
it can be unit-tested; the HTTP endpoints in em_api.py are thin wrappers.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

MODELS_SUBDIR = "oww_models"

# Stems must be shell- and URL-safe: openwakeword derives the prediction
# dict key from the filename, and the dashboard shows it as the label.
_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# 20 MB against a 7.4 MB LiveKit v2 model is 12.6 MB of headroom, and
# aiohttp's transport cap (em_api.create_app) sits above this one either way.
MAX_MODEL_BYTES = 20 * 1024 * 1024

# The two engines a wake model can belong to. `oww` is openwakeword's format:
# one float input of [N, window, 96] embedding frames, one score per call.
# `livekit_v2` is the LiveKit WakeWord streaming format: the ONNX graph carries
# its own state (it takes raw audio chunks and re-emits its state tensors), so
# openwakeword cannot load it and neither can the Echo's firmware — only the
# `livekit-wakeword` Python client can, which is why selecting such a model
# forces owwOnDevice off (em_shadow.forced_mode).
KIND_OWW = "oww"
KIND_LIVEKIT = "livekit_v2"

LIVEKIT_FORMAT = "livekit-wakeword-v2"  # the ONNX metadata `format` its trainer stamps
LIVEKIT_CHUNK = 1280                   # 80 ms at 16 kHz, the v2 graph's audio input


def classify_signature(inputs: list[tuple[str, list, str]],
                       metadata: dict[str, str]) -> str | None:
    """
    Which engine a model with this ONNX signature belongs to, or None when
    neither can load it. `inputs` is [(name, shape, type)] as onnxruntime
    reports them; a dimension is an int, or str/None when dynamic.

    The livekit rule is the input its OWN loader insists on — livekit.wakeword
    .model refuses any session without an input named "chunk" as "not a
    streaming wake word model" — and the trainer stamps `format` into the
    metadata, so either alone identifies v2. The openwakeword rule is the
    signature em_oww_check exists to validate: exactly one float input whose
    last dimension is the 96-wide embedding. Checked in that order: the livekit
    shapes ([1, 1280] audio, state tensors) never match the openwakeword rule
    and vice versa, so the two cannot disagree about a real file.
    """
    for name, shape, kind in inputs:
        if (name == "chunk" and kind == "tensor(float)" and len(shape) == 2
                and shape[-1] == LIVEKIT_CHUNK):
            return KIND_LIVEKIT
    if metadata.get("format") == LIVEKIT_FORMAT:
        return KIND_LIVEKIT
    if (len(inputs) == 1 and inputs[0][2] == "tensor(float)"
            and len(inputs[0][1]) == 3 and inputs[0][1][-1] == 96):
        return KIND_OWW
    return None


# Classified kinds keyed by (resolved path, mtime_ns, size): retraining rewrites
# the file under the same name, and the kind must follow the bytes.
_kind_cache: dict[tuple[str, int, int], str] = {}


def model_kind(model_name: str) -> str:
    """
    Which engine an `owwModel` value points at: KIND_LIVEKIT or KIND_OWW.

    Stock names ("hey_jarvis_v0.1") are openwakeword's by definition. A custom
    file is classified from its signature and cached per (path, mtime, size).
    Classification loads the model through onnxruntime: ~30 ms for an
    openwakeword classifier, ~120 ms for a 7.4 MB livekit_v2 one (measured), so
    call it from an executor thread when the cache may be cold — the wake
    listener's acquire does, and the /api/oww_models read warms every file in
    the dir. Anything that cannot be classified is KIND_OWW: a missing file
    fails the listener's load and reverts, as it has since #869, and
    defaulting an unknown file to openwakeword keeps that behaviour rather
    than inventing a new one.
    """
    name = (model_name or "").strip()
    if not name.endswith(".onnx"):
        return KIND_OWW
    try:
        st = Path(name).stat()
    except OSError:
        return KIND_OWW
    key = (str(Path(name).resolve()), st.st_mtime_ns, st.st_size)
    kind = _kind_cache.get(key)
    if kind is None:
        import em_oww_check  # imports this module; only reached on a cold cache
        kind = em_oww_check.classify(Path(name).read_bytes())[0] or KIND_OWW
        _kind_cache[key] = kind
    return kind


def is_livekit_model(model_name: str) -> bool:
    """Whether this `owwModel` value names a LiveKit v2 file — the question
    every on-device path asks before touching a model (install, hold-back,
    mode). Call off the event loop when the kind cache may be cold, see
    model_kind."""
    return model_kind(model_name) == KIND_LIVEKIT


def prediction_key(model_name: str) -> str:
    """
    The key openwakeword uses in its prediction dict for this owwModel
    value. Stock names ("hey_jarvis_v0.1") key as themselves, but a file
    path keys as the filename stem — scoring a custom model against the
    raw path silently reads 0.0 forever.
    """
    if model_name.endswith(".onnx"):
        return Path(model_name).stem
    return model_name


def scalar_score(value) -> float:
    """
    One score as a plain float, whatever shape the classifier's output has.

    openwakeword reads `prediction[0][0][0]`, a scalar for its own models,
    whose output is [batch, 1]. A model exported with [batch, 1, 1]
    (nanowakeword does) leaves a one-element array there, which compares
    like a number and then fails in an f-string or json.dumps (#869). The
    first element is also what the firmware reads, so both halves score a
    model the same way.
    """
    ravel = getattr(value, "ravel", None)
    return float(ravel()[0] if ravel is not None else value)


# openWakeWord ships its stock models with a version suffix in the FILENAME
# ("hey_jarvis_v0.1.onnx"). That is a packaging convention, not part of the
# wake word, and it has no business in a string a user reads.
_VERSION_SUFFIX = re.compile(r"_v\d+(?:\.\d+)*$")


def display_name(model_name: str) -> str:
    """
    The wake word as a person says it — "hey_jarvis_v0.1" -> "hey jarvis".

    This is NOT cosmetic. Home Assistant compares the `wake_word_phrase` on
    a pipeline start against the STATE of its wake-word select entity, whose
    options are the display names we advertise
    (esphome/assist_satellite.py: `ww_state.state == wake_word_phrase`, and
    select.py's `_wake_words` dict is commented "name -> id"). So the phrase
    we announce and the name we advertise must be the same string, or HA
    matches neither and silently falls back to pipeline 1.

    One function so the two call sites cannot drift.
    """
    return _VERSION_SUFFIX.sub("", prediction_key(model_name)).replace("_", " ")


def models_dir(db_path: str | None = None) -> Path:
    """
    Resolve the custom-models directory: `oww_models/` beside the SQLite
    DB (DB_PATH env, same default as em_controller). Absolute, so the
    stored owwModel path stays valid regardless of the process cwd.
    """
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "echomuse.db")
    return (Path(db_path).resolve().parent / MODELS_SUBDIR)


def safe_model_filename(filename: str) -> str | None:
    """
    Validate an uploaded filename. Returns the sanitised basename
    (always `<stem>.onnx`) or None if unacceptable. Rejects path
    separators, hidden files, and exotic characters outright rather
    than trying to rewrite them.
    """
    base = os.path.basename(filename or "")
    if not base.endswith(".onnx"):
        return None
    stem = base[: -len(".onnx")]
    if not _STEM_RE.match(stem):
        return None
    return base


def scan(directory: Path | None = None) -> list[dict]:
    """
    List custom models: [{name, file, path, size, mtime}], name-sorted.
    `path` is the absolute path to store in owwModel config; `name` is
    the display label (filename stem). Missing dir → empty list.
    """
    directory = directory if directory is not None else models_dir()
    if not directory.is_dir():
        return []
    out = []
    for f in sorted(directory.glob("*.onnx")):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({
            "name":  f.stem,
            "file":  f.name,
            "path":  str(f.resolve()),
            "size":  st.st_size,
            "mtime": int(st.st_mtime),
        })
    return out


def in_use_by(model_path: str, configs: dict[str, dict]) -> list[str]:
    """
    Which config scopes reference this model path? `configs` maps a
    scope label (device id, or "global") to its config dict. Compares
    resolved paths so `/app/data/oww_models/x.onnx` and a symlinked
    spelling of the same file both match.
    """
    target = str(Path(model_path).resolve())
    users = []
    for scope, cfg in configs.items():
        ref = (cfg or {}).get("owwModel") or ""
        if not ref.endswith(".onnx"):
            continue  # stock model name, not a file path
        if str(Path(ref).resolve()) == target:
            users.append(scope)
    return users


def resources_dir() -> Path | None:
    """
    openwakeword's bundled models directory, found WITHOUT importing the
    package: its __init__ imports a training helper that pulls in
    scikit-learn and scipy, ~120 MB and ~2 s, which a controller whose Echoes
    all detect their own wake word never needs. None when it is not installed.
    """
    mod = sys.modules.get("openwakeword")
    if mod is not None and getattr(mod, "__file__", None):
        base = Path(mod.__file__).parent
    else:
        try:
            spec = importlib.util.find_spec("openwakeword")
        except (ImportError, ValueError):
            return None
        if spec is None or not spec.submodule_search_locations:
            return None
        base = Path(list(spec.submodule_search_locations)[0])
    d = base / "resources" / "models"
    return d if d.is_dir() else None


def model_class():
    """openwakeword's Model, imported on first use. Call it only from the
    executor threads that build a model: the import costs ~120 MB and ~2 s,
    and a controller whose Echoes all detect their own wake word never
    builds one."""
    from openwakeword.model import Model
    return Model
