"""
Satellite port allocation, and the base that keeps two controllers apart.

Home Assistant keys an ESPHome device on its host and port. The GA and Early
Access add-ons have separate /data, so each has its own database and its own
port counter, and both start at 16001 — which means a config entry left over
from one channel reaches whichever device now holds that number in the other.

Measured 2026-08-19: after a channel switch, every satellite entity sat
unavailable for a day. Wake words still fired and the ring still lit, and
every turn died in milliseconds because there was no HA pipeline behind it.

EM_ESPHOME_PORT_BASE separates the ranges. It is applied as a floor at
allocation time rather than as a seed, which is what makes it safe to change
on an established install: the counter only moves forwards, so it can never
be pushed onto a port some device is already using.
"""

import importlib
import sqlite3

import pytest

import em_db


def _db(tmp_path, monkeypatch, base=None):
    """A fresh migrated database, with em_db reloaded under `base`."""
    if base is None:
        monkeypatch.delenv("EM_ESPHOME_PORT_BASE", raising=False)
    else:
        monkeypatch.setenv("EM_ESPHOME_PORT_BASE", str(base))
    importlib.reload(em_db)

    p = str(tmp_path / "em.db")
    em_db.init(p)
    return p


def _add(path, device_id):
    c = sqlite3.connect(path)
    c.execute("INSERT INTO devices (device_id, label, approved) VALUES (?, ?, 1)",
              (device_id, device_id))
    c.commit()
    c.close()


@pytest.fixture(autouse=True)
def _restore_em_db():
    # Other modules hold `import em_db`; leave the shared module as found.
    yield
    importlib.reload(em_db)


# ── The default is unchanged ──────────────────────────────────────────────────

def test_a_fresh_database_still_starts_at_16001(tmp_path, monkeypatch):
    """
    Every fielded install allocated from 16001, and the ports are persisted —
    so the unset default has to stay exactly where it was or an upgrade
    renumbers a fleet HA already has entries for.
    """
    p = _db(tmp_path, monkeypatch)
    _add(p, "A")
    assert em_db.assign_esphome_port("A") == 16001


# ── The base ──────────────────────────────────────────────────────────────────

def test_a_fresh_database_allocates_from_the_configured_base(tmp_path, monkeypatch):
    p = _db(tmp_path, monkeypatch, base=16101)
    _add(p, "A")
    _add(p, "B")
    assert em_db.assign_esphome_port("A") == 16101
    assert em_db.assign_esphome_port("B") == 16102


def test_the_ble_range_follows_the_voice_base(tmp_path, monkeypatch):
    """
    BLE proxies are derived, not allocated, so separating the voice bases is
    the whole change — 16101 has to give 17101 with no second setting.
    """
    p = _db(tmp_path, monkeypatch, base=16101)
    _add(p, "A")
    em_db.assign_esphome_port("A")
    assert em_db.ensure_ble_proxy_port("A") == 16101 + em_db.BLE_PORT_OFFSET


def test_the_two_channel_defaults_do_not_overlap():
    """
    The property the whole change exists for, stated against the numbers the
    two add-ons actually ship — 100 voice ports apart, and BLE derived above
    both, so neither range can reach the other at any plausible fleet size.
    """
    ga, ea = 16001, 16101
    assert ea - ga >= 100
    assert ga + em_db.BLE_PORT_OFFSET > ea  # BLE sits above every voice port


# ── A floor, not a seed ───────────────────────────────────────────────────────

def test_raising_the_base_leaves_existing_devices_where_they_are(tmp_path,
                                                                 monkeypatch):
    """
    The reason this is a floor. A device's port is persisted and HA holds a
    config entry against it, so changing the base must move only what has not
    been handed out yet.
    """
    p = _db(tmp_path, monkeypatch)
    _add(p, "A")
    assert em_db.assign_esphome_port("A") == 16001

    monkeypatch.setenv("EM_ESPHOME_PORT_BASE", "16101")
    importlib.reload(em_db)
    em_db.init(p)

    _add(p, "B")
    assert em_db.get_esphome_port("A") == 16001, "an existing device was renumbered"
    assert em_db.assign_esphome_port("B") == 16101


def test_lowering_the_base_never_reissues_a_port(tmp_path, monkeypatch):
    """
    A base below the counter must be ignored rather than obeyed. Obeying it
    would hand a second device a port another one is already listening on,
    which is the exact misrouting the base exists to prevent — arriving from
    the other direction.
    """
    p = _db(tmp_path, monkeypatch, base=16101)
    _add(p, "A")
    assert em_db.assign_esphome_port("A") == 16101

    monkeypatch.setenv("EM_ESPHOME_PORT_BASE", "16001")
    importlib.reload(em_db)
    em_db.init(p)

    _add(p, "B")
    assert em_db.assign_esphome_port("B") == 16102


# ── Bad input ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["", "  ", "sixteen thousand", "16101.5",
                                 "80", "70000", "-1"])
def test_an_unusable_base_falls_back_rather_than_refusing_to_start(raw,
                                                                   monkeypatch):
    """
    A controller that would not start over a stray port number is a worse
    outcome than one that allocates where it always did — and on the add-on
    the value arrives from a text field.
    """
    monkeypatch.setenv("EM_ESPHOME_PORT_BASE", raw)
    importlib.reload(em_db)
    assert em_db.ESPHOME_PORT_BASE == em_db.ESPHOME_PORT_BASE_DEFAULT
