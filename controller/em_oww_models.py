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

import os
import re
from pathlib import Path

MODELS_SUBDIR = "oww_models"

# Stems must be shell- and URL-safe: openwakeword derives the prediction
# dict key from the filename, and the dashboard shows it as the label.
_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

MAX_MODEL_BYTES = 20 * 1024 * 1024  # stock models are ~1 MB; 20 MB is generous


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
