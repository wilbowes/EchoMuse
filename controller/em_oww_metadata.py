"""Read optional ONNX identity metadata without changing scoring model IDs.

Runtime imports stay lazy: filename fallback and metadata validation can run
in the minimal controller test environment.
"""
from dataclasses import dataclass
from functools import lru_cache
import logging
from pathlib import Path
import re

import em_oww_models

log = logging.getLogger("echomuse.owwmetadata")  # not __name__ (#378)
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")


@dataclass(frozen=True)
class WakeWord:
    name: str
    languages: tuple[str, ...] = ("en",)


def from_metadata(model_name: str, metadata: dict) -> WakeWord:
    """Invalid or missing optional fields retain the stock naming convention."""
    name = metadata.get("wake_word")
    if not isinstance(name, str) or not name.strip():
        name = em_oww_models.display_name(model_name)
    language = metadata.get("language")
    languages = (language.replace("_", "-"),) if (
        isinstance(language, str) and _LANGUAGE.fullmatch(language)
    ) else ("en",)
    return WakeWord(name.strip(), languages)


@lru_cache(maxsize=32)
def _read(path: str, mtime_ns: int, size: int, inode: int) -> dict:
    # Loading is bounded to one small classifier and one runtime thread. The
    # file identity invalidates the cache when an upload replaces the model.
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(path, sess_options=options,
                                   providers=["CPUExecutionProvider"])
    return session.get_modelmeta().custom_metadata_map


def resolve(model_name: str) -> WakeWord:
    if not model_name.endswith(".onnx"):
        return from_metadata(model_name, {})
    try:
        path = Path(model_name).resolve()
        stat = path.stat()
        metadata = _read(str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except Exception as exc:
        # Metadata must never make an otherwise supported model unusable.
        log.warning("Cannot read wake-word metadata for %s: %s", model_name, exc)
        metadata = {}
    return from_metadata(model_name, metadata)
