"""
The ESPHome identity: what Home Assistant keys its device registry on.

Two things are being pinned. The derivation must produce a valid, unique,
locally-administered address — the bug @lennart24 found in #212/#217 is that
it did not. And the value must be a stored FACT rather than a function, so
that fixing the derivation does not move every device that already works.
"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

import em_db as db


@pytest.fixture()
def fresh_db(tmp_path):
    path = tmp_path / "test.db"
    db.init(str(path))
    yield db
    if db._conn is not None:
        db._conn.close()
        db._conn = None


# ── The derivation ────────────────────────────────────────────────────────────

def test_serials_differing_only_in_stripped_characters_no_longer_collide():
    """
    The bug. The old derivation kept hex characters only, so devices from one
    batch differing in the trailing letters became the same address and Home
    Assistant treated two Echoes as one.
    """
    a = db.esphome_mac_for("G090LF1180440C9K")
    b = db.esphome_mac_for("G090LF1180440C9R")
    assert db._legacy_serialno_to_mac("G090LF1180440C9K") == \
           db._legacy_serialno_to_mac("G090LF1180440C9R")   # the old bug
    assert a != b


def test_every_address_is_a_valid_locally_administered_unicast_mac():
    """
    Two different bits. 0x02 is locally administered — the private-address
    equivalent. Bit 0 is unicast/multicast and must stay CLEAR: a raw hash
    sets it half the time, which is not a valid unicast address at all.

    Guaranteed by the fixed prefix rather than by masking, so there is no bit
    for a later change to forget.
    """
    for serial in ("G090LF1180440C95", "G0K0XY1180440C9K", "x", "", "ZZZZ"):
        mac = db.esphome_mac_for(serial)
        first = int(mac.split(":")[0], 16)
        assert first & 0x02, f"{mac} is not locally administered"
        assert not first & 0x01, f"{mac} has the multicast bit set"
        assert len(mac.split(":")) == 6


def test_the_derivation_is_stable():
    """It has to survive restarts and rebuilds, or HA loses the device."""
    assert db.esphome_mac_for("G090LF1180440C95") == \
           db.esphome_mac_for("G090LF1180440C95")


# ── Assign once, then never move ──────────────────────────────────────────────

def test_a_mac_is_assigned_once_and_kept(fresh_db):
    db.register_new_device("G090LF1180440C95", "10.0.0.9", "v2.12.0")
    first = db.get_esphome_mac("G090LF1180440C95")
    assert first == db.get_esphome_mac("G090LF1180440C95")
    row = db.get_device("G090LF1180440C95")
    assert row["esphome_mac"] == first


def test_a_stored_mac_wins_over_the_derivation(fresh_db):
    """
    The whole point of storing it. A device registered under an older scheme
    keeps the identity Home Assistant already holds, whatever the current
    derivation would say.
    """
    db.register_new_device("G090LF1180440C95", "10.0.0.9", "v2.12.0")
    with db._tx() as conn:
        conn.execute("UPDATE devices SET esphome_mac = ? WHERE device_id = ?",
                     ("DE:AD:BE:EF:00:01", "G090LF1180440C95"))
    assert db.get_esphome_mac("G090LF1180440C95") == "DE:AD:BE:EF:00:01"
    assert db.get_esphome_mac("G090LF1180440C95") != \
           db.esphome_mac_for("G090LF1180440C95")


def test_an_unregistered_device_still_gets_an_answer(fresh_db):
    """
    ESPHome servers can be built before the device row exists, so this must
    not raise — it returns the value it would store.
    """
    assert db.get_esphome_mac("NEVERSEEN") == db.esphome_mac_for("NEVERSEEN")


# ── The v19 migration ─────────────────────────────────────────────────────────
#
# Built by reintroducing the situation: devices already in the database under
# the old derivation, including a colliding pair. The migration's job is that
# nothing which works today moves.

def _legacy_fleet(tmp_path, serials):
    """A v18 database holding devices, then migrated to current."""
    import sqlite3
    path = tmp_path / "legacy.db"
    db.init(str(path))
    for i, s in enumerate(serials):
        db.register_new_device(s, f"10.0.0.{i+2}", "v2.12.0")
    # Rewind to v18, as a real pre-v19 database is.
    #
    # EVERY column added after v18 has to go, not just esphome_mac: init()
    # re-runs all of MIGRATIONS[18:], and an ALTER TABLE ADD COLUMN that
    # finds its column already there fails the whole migration and takes
    # init() down with it. Dropping only this test's own column made these
    # cases fail the moment v20 was appended, which is a fragile helper
    # rather than a real regression — so the set is derived from the
    # migration SQL and needs no editing next time.
    import re
    with db._tx() as conn:
        conn.execute("UPDATE devices SET esphome_mac = NULL")
        for sql in db.MIGRATIONS[18:]:
            for table, col in re.findall(
                    r"ALTER TABLE (\w+)\s+ADD COLUMN (\w+)", sql):
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        conn.execute("UPDATE system_config SET value = '18' WHERE key = 'schema_version'")
    db._conn.close(); db._conn = None
    db.init(str(path))            # runs v19 + its fixup
    return {s: db.get_device(s)["esphome_mac"] for s in serials}


def test_a_device_with_a_unique_identity_keeps_it(tmp_path):
    """
    The reason this is a fixup and not a DEFAULT. Home Assistant has already
    registered these devices under the old value; a new one orphans the device
    row and every automation referencing its entities.
    """
    serials = ["G090LF1180440C95", "G0K0XY1180440C11"]
    macs = _legacy_fleet(tmp_path, serials)
    for s in serials:
        assert macs[s] == db._legacy_serialno_to_mac(s)


def test_a_colliding_pair_is_split_and_the_oldest_keeps_its_identity(tmp_path):
    """
    The bug being fixed. Both devices derived the same address, so HA had one
    device row for two Echoes. The first-seen one keeps it — it is the one HA
    actually holds — and the other, which was being overwritten, gets a new
    identity and appears properly for the first time.
    """
    a, b = "G090LF1180440C9K", "G090LF1180440C9R"
    assert db._legacy_serialno_to_mac(a) == db._legacy_serialno_to_mac(b)
    macs = _legacy_fleet(tmp_path, [a, b])
    assert macs[a] == db._legacy_serialno_to_mac(a)      # oldest keeps it
    assert macs[b] == db.esphome_mac_for(b)              # the overwritten one moves
    assert macs[a] != macs[b]


def test_the_migration_leaves_every_device_with_a_distinct_identity(tmp_path):
    serials = ["G090LF1180440C95", "G090LF1180440C9K", "G090LF1180440C9R",
               "G090LF1180440FRK", "G090LF1180440FRR"]
    macs = _legacy_fleet(tmp_path, serials)
    assert len(set(macs.values())) == len(serials)


def test_migrations_are_append_only():
    """
    The stored schema_version is an index into MIGRATIONS, so appending to a
    deployed entry corrupts every database that already ran it. v19 must be a
    new entry, and v18 must be untouched.

    The length is pinned deliberately. Bumping it is the one moment somebody
    adding a migration has to look at this file and confirm they APPENDED
    rather than edited — which is the mistake this guards, and the one that
    broke every stats write and disconnect-looped the fleet when it happened.
    """
    assert len(db.MIGRATIONS) == 20
    assert "esphome_mac" in db.MIGRATIONS[18]
    assert "esphome_mac" not in db.MIGRATIONS[17]
    # v20 is its own entry and did not get appended onto v19's.
    assert "ble_restarts_last" in db.MIGRATIONS[19]
    assert "ble_restarts_last" not in db.MIGRATIONS[18]
