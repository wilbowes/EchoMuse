"""
em_provider_keys.py — reversible storage for cloud provider API keys.

Provider keys must be SENT to the provider at query time, so unlike the
console password (em_console_pw: salted hash, write-only) they cannot be
hashed — this is the first reversible controller secret. Protection is
instead:

- a JSON file with 0600 permissions (os.open, the em_pki precedent),
  sitting beside the SQLite database on the existing data volume;
- never logging key material, and never returning a key for display
  (callers get SENTINEL/bool, the em_console_pw read pattern);
- `*_api_key`-shaped naming, so em_support's auto-redact (any key NAME
  containing psk|password|token|secret|key) catches these if they ever
  reach a bundle — asserted by test, not by hope.

Unknown provider ids read as absent (get_key -> None, has_key -> False)
and deletes are idempotent; empty ids are rejected with ValueError.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("echomuse.provider_keys")

# What a client is shown in place of a stored key, and what it sends back
# unchanged — the em_console_pw pattern. The key itself never leaves this
# module except via get_key, which is for server-side provider calls only.
SENTINEL = "__unchanged__"

STORE_FILENAME = "provider_keys.json"

_lock = threading.Lock()


def _default_store_path() -> Path:
    """`provider_keys.json` beside the SQLite DB (DB_PATH env, same default
    as em_controller and the em_recordings/em_firmware precedents)."""
    db_path = os.environ.get("DB_PATH", "echomuse.db")
    return Path(db_path).resolve().parent / STORE_FILENAME


def _resolve_path(store_path: str | None) -> Path:
    return Path(store_path) if store_path else _default_store_path()


def _check_provider_id(provider_id: str) -> str:
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("provider_id must be a non-empty string")
    return provider_id.strip()


def _read_all(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("Ignoring unreadable provider key store at %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    # os.open's mode applies only at creation; enforce on every write so a
    # file relaxed by a restore or a stray chmod does not stay relaxed.
    os.chmod(path, 0o600)


def set_key(provider_id: str, key: str, *, store_path: str | None = None) -> None:
    """Store (or replace) the API key for a provider. Never logged."""
    provider_id = _check_provider_id(provider_id)
    if not isinstance(key, str) or not key.strip():
        raise ValueError("refusing to store an empty provider api key")
    path = _resolve_path(store_path)
    with _lock:
        data = _read_all(path)
        data[provider_id] = key
        _write_all(data, path)
    log.info("Provider api key stored for %s", provider_id)


def get_key(provider_id: str, *, store_path: str | None = None) -> str | None:
    """The stored API key, or None when absent. Server-side use only —
    never return this to a client."""
    provider_id = _check_provider_id(provider_id)
    with _lock:
        value = _read_all(_resolve_path(store_path)).get(provider_id)
    return value if isinstance(value, str) and value else None


def delete_key(provider_id: str, *, store_path: str | None = None) -> None:
    """Remove a provider's key. Idempotent — unknown ids are a no-op."""
    provider_id = _check_provider_id(provider_id)
    path = _resolve_path(store_path)
    with _lock:
        data = _read_all(path)
        if provider_id not in data:
            return
        del data[provider_id]
        _write_all(data, path)
    log.info("Provider api key deleted for %s", provider_id)


def has_key(provider_id: str, *, store_path: str | None = None) -> bool:
    """Whether a key is stored for a provider. Safe to return to clients."""
    return get_key(provider_id, store_path=store_path) is not None


def masked_list(*, store_path: str | None = None) -> dict[str, bool]:
    """provider_id -> key-is-set. Carries no key material by construction."""
    with _lock:
        data = _read_all(_resolve_path(store_path))
    return {pid: bool(v) for pid, v in data.items() if isinstance(pid, str)}


def for_display(provider_id: str, *, store_path: str | None = None) -> str:
    """What to return to a client in place of the stored key."""
    return SENTINEL if has_key(provider_id, store_path=store_path) else ""


def resolve_write(provider_id: str, value, *, store_path: str | None = None) -> str:
    """
    The display-safe result of a client write for one provider's api key.

    Three cases and no fourth (the em_console_pw shape): the sentinel means
    leave the stored key alone, empty means remove it, and anything else is
    a new key to store. Returns SENTINEL when a key remains, "" when none
    does — never the key.
    """
    provider_id = _check_provider_id(provider_id)
    if value is None:
        return for_display(provider_id, store_path=store_path)
    text = str(value).strip()
    if text == SENTINEL:
        return for_display(provider_id, store_path=store_path)
    if not text:
        delete_key(provider_id, store_path=store_path)
        return ""
    set_key(provider_id, text, store_path=store_path)
    return SENTINEL
