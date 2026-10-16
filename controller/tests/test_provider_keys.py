"""
Tests for em_provider_keys — reversible provider API key storage.

Properties that matter: a stored key comes back verbatim for server-side
use, but no read path aimed at clients ever carries key material; the file
is 0600; and `*_api_key`-shaped names stay redacted out of support bundles.
"""

import json
import os
import stat

import pytest

import em_provider_keys as pk
import em_support


def _store(tmp_path):
    return str(tmp_path / "provider_keys.json")


# ─── round-trip ──────────────────────────────────────────────────────────

def test_set_get_round_trip(tmp_path):
    pk.set_key("openai", "sk-test-value-1", store_path=_store(tmp_path))
    assert pk.get_key("openai", store_path=_store(tmp_path)) == "sk-test-value-1"
    assert pk.has_key("openai", store_path=_store(tmp_path)) is True


def test_set_replaces_existing(tmp_path):
    store = _store(tmp_path)
    pk.set_key("openai", "sk-old", store_path=store)
    pk.set_key("openai", "sk-new", store_path=store)
    assert pk.get_key("openai", store_path=store) == "sk-new"


def test_providers_are_independent(tmp_path):
    store = _store(tmp_path)
    pk.set_key("openai", "sk-a", store_path=store)
    pk.set_key("anthropic", "sk-b", store_path=store)
    assert pk.get_key("openai", store_path=store) == "sk-a"
    pk.delete_key("openai", store_path=store)
    assert pk.get_key("anthropic", store_path=store) == "sk-b"


def test_set_rejects_empty_key(tmp_path):
    with pytest.raises(ValueError):
        pk.set_key("openai", "", store_path=_store(tmp_path))
    with pytest.raises(ValueError):
        pk.set_key("openai", "   ", store_path=_store(tmp_path))


def test_empty_provider_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        pk.set_key("", "sk-x", store_path=_store(tmp_path))
    with pytest.raises(ValueError):
        pk.get_key("  ", store_path=_store(tmp_path))


# ─── resolve_write (sentinel / empty / new) ──────────────────────────────

def test_resolve_write_new_key_stores_and_returns_sentinel(tmp_path):
    store = _store(tmp_path)
    assert pk.resolve_write("openai", "sk-fresh", store_path=store) == pk.SENTINEL
    assert pk.get_key("openai", store_path=store) == "sk-fresh"


def test_resolve_write_sentinel_keeps_existing(tmp_path):
    store = _store(tmp_path)
    pk.set_key("openai", "sk-keep-me", store_path=store)
    assert pk.resolve_write("openai", pk.SENTINEL, store_path=store) == pk.SENTINEL
    assert pk.get_key("openai", store_path=store) == "sk-keep-me"


def test_resolve_write_empty_deletes(tmp_path):
    store = _store(tmp_path)
    pk.set_key("openai", "sk-gone", store_path=store)
    assert pk.resolve_write("openai", "", store_path=store) == ""
    assert pk.has_key("openai", store_path=store) is False
    assert pk.get_key("openai", store_path=store) is None


def test_for_display_never_carries_the_key(tmp_path):
    store = _store(tmp_path)
    assert pk.for_display("openai", store_path=store) == ""
    pk.set_key("openai", "sk-hidden", store_path=store)
    assert pk.for_display("openai", store_path=store) == pk.SENTINEL


# ─── unknown provider ids: absent, not an error ──────────────────────────

def test_unknown_provider_reads_as_absent(tmp_path):
    store = _store(tmp_path)
    assert pk.get_key("nope", store_path=store) is None
    assert pk.has_key("nope", store_path=store) is False
    pk.delete_key("nope", store_path=store)  # idempotent, no raise


# ─── file permissions ────────────────────────────────────────────────────

def test_created_file_is_0600(tmp_path):
    store = _store(tmp_path)
    pk.set_key("openai", "sk-perms", store_path=store)
    mode = stat.S_IMODE(os.stat(store).st_mode)
    assert mode == 0o600


def test_rewrite_enforces_0600(tmp_path):
    store = _store(tmp_path)
    pk.set_key("openai", "sk-first", store_path=store)
    os.chmod(store, 0o644)
    pk.set_key("openai", "sk-second", store_path=store)
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o600


# ─── masked_list leaks no values ─────────────────────────────────────────

def test_masked_list_reports_presence_not_values(tmp_path):
    store = _store(tmp_path)
    pk.set_key("openai", "sk-super-secret-value", store_path=store)
    pk.set_key("anthropic", "sk-other-secret", store_path=store)
    masked = pk.masked_list(store_path=store)
    assert masked == {"openai": True, "anthropic": True}
    assert "sk-super-secret-value" not in json.dumps(masked)
    assert "sk-other-secret" not in json.dumps(masked)


def test_masked_list_empty_store_is_empty(tmp_path):
    assert pk.masked_list(store_path=_store(tmp_path)) == {}


# ─── default path sits beside the DB ─────────────────────────────────────

def test_default_path_sits_beside_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    assert pk._default_store_path() == tmp_path / "provider_keys.json"


# ─── support bundle exclusion ────────────────────────────────────────────

def test_api_key_shaped_names_are_redacted_from_bundles():
    redacted = em_support.redact_config({
        "openai_api_key": "sk-live-secret",
        "anthropic_api_key": "sk-live-secret",
        "owwThreshold": 0.5,
    })
    assert redacted["openai_api_key"] == "<redacted>"
    assert redacted["anthropic_api_key"] == "<redacted>"
    assert redacted["owwThreshold"] == 0.5
    assert "sk-live-secret" not in json.dumps(redacted)
