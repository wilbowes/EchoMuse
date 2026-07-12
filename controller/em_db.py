"""
db.py — EchoMuse Controller persistence layer
==============================================

SQLite-backed storage for device registry, per-device config, logs,
users, sessions, and system configuration.

All public functions are synchronous — they are intended to be called
from asyncio handlers via loop.run_in_executor() when called on the
hot path, or directly for startup/shutdown operations.

Usage:
    import db

    db.init("echomuse.db")          # call once at startup

    device = db.get_device(device_id)
    db.upsert_device_seen(device_id, ip, version)
    db.log_device(device_id, "info", "device", "Connected")
"""

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional

log = logging.getLogger("echomuse.db")

# ─── Default device config ────────────────────────────────────────────────────

DEFAULT_DEVICE_CONFIG = {
    "adcDigitalGain":   88,
    "adcMicpga":        40,
    # micGainDb: fixed digital gain (dB) the device applies to the full
    # 24-bit capture before quantising to the 16-bit stream. Sized from
    # 20h fleet logs (2026-07-07): speech RMS at wake detection was
    # 0.0001–0.0006 FS (~3–20 LSB in 16-bit — the old S24→S16 truncation
    # discarded most of the signal), loudest observed chunk 0.0035 FS, so
    # +24dB (×16) lifts speech into a usable range with ample clipping
    # headroom. Device clamps to [0, 42]; clipped-sample count appears in
    # the device's periodic VAD diag log. Note: the device interprets
    # vadThreshold in pre-gain units (threshold is scaled by the gain
    # internally), so this can be tuned without retuning vadThreshold.
    "micGainDb":        24,
    # AEC (speexdsp, device-side, whole mic path incl. wake stream).
    # Default OFF — enable per-deployment and check the [aec] att= logs.
    # aecDelayMs: 0, measured on hardware 2026-07-08 — the mic side reads
    # 160ms ALSA batches, which eats most of the speaker's write-to-ear
    # latency; the filter tail absorbs the remainder. (The original 250
    # guess made the echo arrive *before* its reference — non-causal, zero
    # cancellation.) aecTailMs is the adaptive filter length (residual
    # delay + room reverb). Device clamps: delay 0–1000, tail 50–500.
    "aecEnabled":       False,
    "aecDelayMs":       0,
    "aecTailMs":        300,
    "startupVolume":    85,
    # vadThreshold: 0.001 (normalised RMS pre-AGC).
    # Q2 fix (2026-07-05 review, tracked as B6): this was drifted to 0.003 in
    # a previous "reconciliation" that got it backwards — 0.003 sits *above*
    # the measured conversational speech range (0.0004–0.0010 at 1.3m per
    # SETUP.md's handoff table), meaning a fresh device or a config reset
    # would fail to gate speech at all at normal distance. 0.001 is the
    # value actually validated during the v2.6.3 speech-quality session and
    # confirmed working in both quiet-office and TV-on-lounge testing — it's
    # also what the dashboard slider's own fallback already defaulted to
    # (config.vadThreshold ?? 0.001 in dashboard.jsx), so this closes a
    # three-way mismatch between the DB default, this comment, and the UI
    # rather than resolving it in favour of the wrong side. Raise to
    # 0.003–0.005 only in genuinely noisy rooms (TV, music) via the
    # dashboard, not as the shipped default.
    "vadThreshold":     0.001,
    "vadSpeechMs":      32,
    # vadSilenceMs: 900ms — up from 600. Prevents premature endpoint on
    # natural mid-sentence pauses. Dashboard UI already defaults to 800;
    # this closes the DB/UI mismatch. Tune up to 1200 if sentences still
    # get clipped; 600 caused the "must finish quickly" behaviour.
    "vadSilenceMs":     900,
    "owwThreshold":     0.3,
    # Barge-in (§3.2, controller-side): wake word spoken during TTS playback
    # cancels it and starts a fresh turn. Requires device AEC (aecEnabled)
    # on — with barge-in the mic streams through playback, and AEC is what
    # stops the device hearing itself. bargeInThreshold is used as-is,
    # deliberately BELOW the normal wake threshold: the echo at the mic is
    # ~25dB louder than the person talking over it, so speech-over-TTS wake
    # scores are inherently depressed (~0.10–0.12 measured), while post-AEC
    # self-echo scores only 0.004 (0.055 worst-case unconverged) — there is
    # no self-trigger risk down to ~0.08.
    "bargeInEnabled":   False,
    "bargeInThreshold": 0.10,
    "owwModel":         "hey_jarvis_v0.1",
    # owwSpeexNs: openwakeword's built-in speexdsp noise suppressor (Q1,
    # 2026-07-05 review). 16kHz-native, applied controller-side, only to
    # the wake-word detection path — cannot affect STT audio since STT
    # never sees it. Distinct from nsEnabled/RNNoise below, which run
    # device-side on the whole pipeline. Defaults False: needs the
    # speexdsp-ns pip package confirmed installable in the Docker build
    # (see review Q1 fix sequence) before enabling fleet-wide; flip on and
    # A/B test wake rate in a noisy room once confirmed.
    "owwSpeexNs":       False,
    # nsAsr: DTLN noise suppression (em_ns.py), controller-side, applied
    # ONLY to the turn audio streamed to HA's STT — the wake stream and
    # all noise-floor measurement stay raw. Helps steady noise (fan, AC,
    # hum) at marginal SNR; does little against competing speech (TV) —
    # that's the beamformer's job. Default off pending A/B validation
    # (2026-07-12); models are vendored into the Docker image, so if the
    # files are missing (bare-metal without NS_MODEL_DIR) the flag
    # degrades to raw streaming with a warning.
    "nsAsr":            False,
    # beamformingEnabled: False — ch6 (centre/omni) for all audio.
    # beamforming=True was routing OWW audio through a perimeter mic selected
    # every 32ms frame, injecting channel-splice discontinuities. The SNR
    # difference between ch6 and the "best" perimeter mic at conversational
    # distance is negligible; the downside (wrong-mic locks, discontinuities)
    # is real. Off = ch6 for everything, consistent with what makes OWW work.
    "beamformingEnabled": False,
    "beamAngle":        -1,
    "eqBands":          [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "eqLoudness":       False,
    # LED ring scene (controller-side rendering — see em_scenes.py).
    # ledListenColor/ledThinkColor only apply when ledScene is "custom".
    "ledScene":         "standard",
    "ledListenColor":   "#00b400",
    "ledThinkColor":    "#00c800",
    # Pipeline toggles — both default on. Disable via dashboard for A/B testing.
    # nsEnabled: RNNoise noise suppression. Running at 16kHz (wrong rate for
    # the model — see P0-3). Disable to A/B test whether it's helping or hurting.
    # agcEnabled: automatic gain control. Disable to hear raw mic levels.
    "nsEnabled":        True,
    "agcEnabled":       True,
}

# Maximum log rows retained per device. Older rows are pruned on insert.
LOG_RETENTION = 10_000

# ─── Migrations ───────────────────────────────────────────────────────────────
#
# Rules:
#   - Append-only. Never edit an existing entry.
#   - Each migration must update schema_version as its final statement.
#   - Use CREATE TABLE IF NOT EXISTS / INSERT OR IGNORE for idempotency.
#   - SQLite ALTER TABLE only supports ADD COLUMN. Renaming or dropping
#     columns requires a create-copy-drop migration.

MIGRATIONS: list[str] = [
    # ── v1 — initial schema ──────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id         TEXT    PRIMARY KEY,
        label             TEXT,
        approved          INTEGER NOT NULL DEFAULT 0,
        ip                TEXT,
        firmware_ver      TEXT,
        firmware_previous TEXT,
        first_seen        INTEGER,
        last_seen         INTEGER,
        config            TEXT    NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS device_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id  TEXT    NOT NULL,
        ts         INTEGER NOT NULL,
        level      TEXT    NOT NULL,
        source     TEXT    NOT NULL,
        message    TEXT    NOT NULL,
        FOREIGN KEY (device_id) REFERENCES devices(device_id)
    );
    CREATE INDEX IF NOT EXISTS idx_device_logs_device_ts
        ON device_logs(device_id, ts DESC);

    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT    UNIQUE NOT NULL,
        password_hash TEXT    NOT NULL,
        role          TEXT    NOT NULL DEFAULT 'readonly',
        created_at    INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT    PRIMARY KEY,
        user_id    INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS system_config (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    INSERT OR IGNORE INTO system_config VALUES ('schema_version',        '1');
    INSERT OR IGNORE INTO system_config VALUES ('device_approval',       'strict');
    INSERT OR IGNORE INTO system_config VALUES ('session_expiry_days',   '30');
    INSERT OR IGNORE INTO system_config VALUES ('update_check_interval', '3600');
    INSERT OR IGNORE INTO system_config VALUES ('github_repo',           'wilbowes/EchoMuse');
    INSERT OR IGNORE INTO system_config VALUES ('latest_version',        NULL);
    INSERT OR IGNORE INTO system_config VALUES ('latest_binary_url',     NULL);
    INSERT OR IGNORE INTO system_config VALUES ('last_update_check',     NULL);
    """,

    # ── v2 — ESPHome native API integration ─────────────────────────────────
    """
    ALTER TABLE devices ADD COLUMN esphome_api_port INTEGER;
    ALTER TABLE devices ADD COLUMN esphome_noise_psk TEXT;

    INSERT OR IGNORE INTO system_config VALUES ('next_esphome_port', '16001');

    UPDATE system_config SET value = '2' WHERE key = 'schema_version';
    """,

    # ── v3 — Global device config and per-device override flag ───────────────
    #
    # use_global_config=1 (default): device inherits fleet-wide defaults.
    # use_global_config=0: device has its own config stored in the config column.
    #
    # global_device_config: JSON blob in system_config, same shape as
    # DEFAULT_DEVICE_CONFIG. Seeded from DEFAULT_DEVICE_CONFIG at migration time
    # so existing installs get the same values they were already using.
    f"""
    ALTER TABLE devices ADD COLUMN use_global_config INTEGER NOT NULL DEFAULT 1;

    INSERT OR IGNORE INTO system_config VALUES ('global_device_config', '{json.dumps(DEFAULT_DEVICE_CONFIG)}');

    UPDATE system_config SET value = '3' WHERE key = 'schema_version';
    """,
]

# ─── Connection management ────────────────────────────────────────────────────

_db_path: str = ""
_conn: Optional[sqlite3.Connection] = None

# SQLite WAL mode allows concurrent readers, but writes must be serialised.
# run_in_executor dispatches db calls across multiple threads sharing the same
# connection; without a lock, concurrent _tx sequences can interleave — thread B's
# commit landing mid-transaction-A, or a rollback eating another thread's writes.
_write_lock = threading.Lock()


def init(path: str = "echomuse.db") -> None:
    """
    Initialise the database. Must be called once at startup before any
    other db function is used.

    Opens a single persistent connection, enables WAL mode and foreign
    key enforcement, then runs any pending migrations.
    """
    global _db_path, _conn
    _db_path = path
    log.info(f"Opening database: {os.path.abspath(path)}")
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.commit()
    _migrate(_conn)
    log.info("Database ready")


@contextmanager
def _tx():
    """
    Context manager for a write transaction.

    Commits on clean exit, rolls back on any exception and re-raises.
    Acquires _write_lock to serialise concurrent executor-thread writes on the
    shared connection — SQLite WAL allows concurrent reads but not concurrent
    write transactions from the same connection object.
    """
    assert _conn is not None, "db.init() has not been called"
    with _write_lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def _q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Execute a read query and return all rows."""
    assert _conn is not None, "db.init() has not been called"
    return _conn.execute(sql, params).fetchall()


def _q1(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    """Execute a read query and return at most one row."""
    assert _conn is not None, "db.init() has not been called"
    return _conn.execute(sql, params).fetchone()


# ─── Migrations ───────────────────────────────────────────────────────────────

def _migrate(conn: sqlite3.Connection) -> None:
    """Run any pending migrations in order."""
    # Determine current schema version. On a brand-new database the
    # system_config table doesn't exist yet, so we catch that case.
    try:
        row = conn.execute(
            "SELECT value FROM system_config WHERE key = 'schema_version'"
        ).fetchone()
        current = int(row[0]) if row else 0
    except sqlite3.OperationalError:
        current = 0  # fresh database — system_config doesn't exist yet

    pending = MIGRATIONS[current:]
    if not pending:
        log.debug(f"Schema is current at v{current}")
        return

    log.info(f"Running {len(pending)} migration(s) from v{current}")
    for i, sql in enumerate(pending):
        version = current + i + 1
        log.info(f"Applying migration v{version}")
        try:
            conn.executescript(sql)
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"Migration v{version} failed: {e}")
            raise RuntimeError(f"Database migration v{version} failed — cannot start") from e

    new_version = current + len(pending)
    log.info(f"Schema migrated to v{new_version}")


# ─── Device registry ──────────────────────────────────────────────────────────

def get_device(device_id: str) -> Optional[sqlite3.Row]:
    """
    Return the device row for device_id, or None if not registered.

    Row fields: device_id, label, approved, ip, firmware_ver,
                firmware_previous, first_seen, last_seen, config (JSON str)
    """
    return _q1(
        "SELECT * FROM devices WHERE device_id = ?",
        (device_id,),
    )


def get_all_devices() -> list[sqlite3.Row]:
    """Return all device rows ordered by first_seen."""
    return _q("SELECT * FROM devices ORDER BY first_seen ASC")


def get_pending_devices() -> list[sqlite3.Row]:
    """Return devices that have connected but not yet been approved."""
    return _q(
        "SELECT * FROM devices WHERE approved = 0 ORDER BY first_seen ASC"
    )


def register_new_device(device_id: str, ip: str, version: Optional[str]) -> None:
    """
    Insert a new device row with approved=0 (pending).

    Called when an unknown device_id connects for the first time.
    Config is seeded from DEFAULT_DEVICE_CONFIG.
    """
    now = _now()
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO devices
                (device_id, label, approved, ip, firmware_ver, first_seen, last_seen, config)
            VALUES (?, NULL, 0, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                ip,
                version,
                now,
                now,
                json.dumps(DEFAULT_DEVICE_CONFIG),
            ),
        )
    log.info(f"[db] New device registered (pending): {device_id}")


def approve_device(device_id: str, label: str, config: Optional[dict] = None) -> None:
    """
    Approve a pending device and optionally set label and config.

    If config is not supplied the existing seeded config is kept.
    Raises ValueError if the device is not found.
    """
    device = get_device(device_id)
    if device is None:
        raise ValueError(f"Device not found: {device_id}")

    effective_config = json.dumps(config) if config is not None else device["config"]

    with _tx() as conn:
        conn.execute(
            """
            UPDATE devices
            SET approved = 1,
                label    = ?,
                config   = ?
            WHERE device_id = ?
            """,
            (label, effective_config, device_id),
        )
    log.info(f"[db] Device approved: {device_id} label={label!r}")


def upsert_device_seen(
    device_id: str,
    ip: str,
    version: Optional[str],
) -> None:
    """
    Update ip, firmware_ver, and last_seen for a known device on each connection.

    Does not touch approval status, label, or config.
    """
    with _tx() as conn:
        conn.execute(
            """
            UPDATE devices
            SET ip           = ?,
                firmware_ver = ?,
                last_seen    = ?
            WHERE device_id = ?
            """,
            (ip, version, _now(), device_id),
        )


def set_device_label(device_id: str, label: str) -> None:
    """Update the human-readable label for a device."""
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET label = ? WHERE device_id = ?",
            (label, device_id),
        )


def set_device_config(device_id: str, config: dict) -> None:
    """
    Persist updated config for a device.

    The caller is responsible for immediately pushing the config to the
    live device over the control WebSocket if it is currently connected.
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET config = ? WHERE device_id = ?",
            (json.dumps(config), device_id),
        )


def get_device_config(device_id: str) -> dict:
    """
    Return the config dict for a device.

    Falls back to DEFAULT_DEVICE_CONFIG if the device is not found or
    config is empty/invalid — this should not normally happen.
    """
    row = _q1("SELECT config FROM devices WHERE device_id = ?", (device_id,))
    if row is None:
        return dict(DEFAULT_DEVICE_CONFIG)
    try:
        return json.loads(row["config"]) or dict(DEFAULT_DEVICE_CONFIG)
    except (json.JSONDecodeError, TypeError):
        log.warning(f"[db] Invalid config JSON for {device_id} — using defaults")
        return dict(DEFAULT_DEVICE_CONFIG)


def get_global_device_config() -> dict:
    """
    Return the fleet-wide default device config.

    Falls back to DEFAULT_DEVICE_CONFIG if the key is missing or unparseable
    (should only occur on a fresh DB before migration v3 has run).
    """
    row = _q1("SELECT value FROM system_config WHERE key = 'global_device_config'")
    if row is None or not row["value"]:
        return dict(DEFAULT_DEVICE_CONFIG)
    try:
        stored = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        log.warning("[db] Invalid global_device_config JSON — using defaults")
        return dict(DEFAULT_DEVICE_CONFIG)
    if not stored:
        return dict(DEFAULT_DEVICE_CONFIG)
    # Underlay defaults so keys added after the stored config was last
    # saved (e.g. micGainDb) are still pushed with their default value
    # instead of silently falling back to whatever the device binary's
    # env default happens to be.
    return {**DEFAULT_DEVICE_CONFIG, **stored}


def set_global_device_config(config: dict) -> None:
    """Persist updated fleet-wide default device config."""
    with _tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_config (key, value) VALUES ('global_device_config', ?)",
            (json.dumps(config),),
        )


def get_effective_device_config(device_id: str) -> dict:
    """
    Return the config that should be pushed to a device.

    If use_global_config=1 (or the device is not found), returns the
    fleet-wide global config — but always overrides startupVolume with
    the device's own stored value. Volume is hardware state set at
    provisioning time; it should never be clobbered by a fleet default.

    If use_global_config=0, returns the device's own config entirely.

    This is the authoritative source for what config a device should
    actually run — use it in device_connected() and any config-push path.
    """
    row = _q1(
        "SELECT use_global_config, config FROM devices WHERE device_id = ?",
        (device_id,),
    )
    if row is None:
        return get_global_device_config()

    try:
        per_device = json.loads(row["config"]) or {}
    except (json.JSONDecodeError, TypeError):
        log.warning(f"[db] Invalid config JSON for {device_id} — using global")
        per_device = {}

    if row["use_global_config"]:
        config = get_global_device_config()
        # startupVolume is per-device hardware state — never inherit from global
        if "startupVolume" in per_device:
            config["startupVolume"] = per_device["startupVolume"]
        return config
    else:
        return per_device or get_global_device_config()


def set_device_use_global(device_id: str, enabled: bool) -> None:
    """
    Set the use_global_config flag for a device.

    When enabling (reverting to global): also resets the device's own
    config column to a copy of the current global config, so the stored
    value stays coherent if the flag is toggled again later.

    When disabling (enabling per-device override): the config column is
    left as-is; the caller is expected to immediately follow with
    set_device_config() to write the desired override values.
    """
    with _tx() as conn:
        if enabled:
            global_cfg = get_global_device_config()
            conn.execute(
                "UPDATE devices SET use_global_config = 1, config = ? WHERE device_id = ?",
                (json.dumps(global_cfg), device_id),
            )
        else:
            conn.execute(
                "UPDATE devices SET use_global_config = 0 WHERE device_id = ?",
                (device_id,),
            )
    log.info(f"[db] use_global_config={'1' if enabled else '0'}: {device_id}")


def set_firmware_previous(device_id: str, version: Optional[str]) -> None:
    """
    Record the version that server.old holds after an OTA update.

    Set to the old running version before the update is applied.
    Set to None once server.old is pruned or a rollback completes.
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET firmware_previous = ? WHERE device_id = ?",
            (version, device_id),
        )


def delete_device(device_id: str) -> None:
    """
    Remove a device and all its logs from the registry.

    This is a hard delete — use with care. Logs are removed first to
    satisfy the foreign key constraint.
    """
    with _tx() as conn:
        conn.execute("DELETE FROM device_logs WHERE device_id = ?", (device_id,))
        conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
    log.info(f"[db] Device deleted: {device_id}")


# ─── ESPHome port allocation ──────────────────────────────────────────────────

def get_esphome_port(device_id: str) -> Optional[int]:
    """
    Return the ESPHome API port assigned to this device, or None if unassigned.

    A None return means the device has never been assigned a port in esphome
    mode — call assign_esphome_port() to allocate one.
    """
    row = _q1("SELECT esphome_api_port FROM devices WHERE device_id = ?", (device_id,))
    if row is None:
        return None
    return row["esphome_api_port"]  # may be None (unassigned)


def assign_esphome_port(device_id: str) -> int:
    """
    Allocate and persist an ESPHome API port for this device.

    Takes the next available port from next_esphome_port in system_config,
    increments the counter, persists both atomically, and returns the
    allocated port.

    Port allocation is monotonically increasing and never reuses freed ports
    (see ESPHOME_SPEC.md §2.2 for the rationale — sparse range is intentional
    to prevent silent misrouting if HA still holds a stale config entry for a
    deprovisioned device's old port number).

    Raises ValueError if the device is not found.
    Raises RuntimeError if a port is already assigned — caller should use
    get_esphome_port() first to check.
    """
    with _tx() as conn:
        row = conn.execute(
            "SELECT esphome_api_port FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Device not found: {device_id}")
        if row["esphome_api_port"] is not None:
            raise RuntimeError(
                f"Device {device_id} already has ESPHome port {row['esphome_api_port']} — "
                f"use get_esphome_port() to retrieve it"
            )

        next_row = conn.execute(
            "SELECT value FROM system_config WHERE key = 'next_esphome_port'"
        ).fetchone()
        port = int(next_row["value"])

        conn.execute(
            "UPDATE devices SET esphome_api_port = ? WHERE device_id = ?",
            (port, device_id),
        )
        conn.execute(
            "UPDATE system_config SET value = ? WHERE key = 'next_esphome_port'",
            (str(port + 1),),
        )

    log.info(f"[db] ESPHome port assigned: {device_id} → {port}")
    return port


def free_esphome_port(device_id: str) -> None:
    """
    Clear the ESPHome API port assignment for a device.

    Called on device deprovisioning. The freed port number is NOT returned
    to the pool — next_esphome_port only ever increments (see assign_esphome_port).
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE devices SET esphome_api_port = NULL WHERE device_id = ?",
            (device_id,),
        )
    log.info(f"[db] ESPHome port freed: {device_id}")


# ─── Device logs ──────────────────────────────────────────────────────────────

def log_device(
    device_id: str,
    level: str,
    source: str,
    message: str,
) -> None:
    """
    Append a log entry for a device and prune old entries.

    level:  'info' | 'warn' | 'error'
    source: 'device' | 'controller'

    Pruning keeps the most recent LOG_RETENTION rows per device.
    The extra DELETE is cheap on SQLite at this row count.
    """
    now_ms = int(time.time() * 1000)
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO device_logs (device_id, ts, level, source, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (device_id, now_ms, level, source, message),
        )
        # Prune: delete all but the most recent LOG_RETENTION rows for this device.
        conn.execute(
            """
            DELETE FROM device_logs
            WHERE device_id = ?
              AND id NOT IN (
                  SELECT id FROM device_logs
                  WHERE device_id = ?
                  ORDER BY ts DESC
                  LIMIT ?
              )
            """,
            (device_id, device_id, LOG_RETENTION),
        )


def get_device_logs(
    device_id: str,
    limit: int = 100,
    before_ts: Optional[int] = None,
) -> list[sqlite3.Row]:
    """
    Return log entries for a device in reverse-chronological order.

    limit:     maximum rows to return (capped at 1000)
    before_ts: if set, return only entries with ts < before_ts
               (cursor-based pagination — pass the ts of the last row
               from the previous page)
    """
    limit = min(limit, 1000)
    if before_ts is not None:
        return _q(
            """
            SELECT * FROM device_logs
            WHERE device_id = ? AND ts < ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (device_id, before_ts, limit),
        )
    return _q(
        """
        SELECT * FROM device_logs
        WHERE device_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (device_id, limit),
    )


# ─── Users ────────────────────────────────────────────────────────────────────

def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    """Return a user row by username, or None."""
    return _q1("SELECT * FROM users WHERE username = ?", (username,))


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    """Return a user row by id, or None."""
    return _q1("SELECT * FROM users WHERE id = ?", (user_id,))


def create_user(username: str, password_hash: str, role: str = "readonly") -> int:
    """
    Insert a new user row and return the new user id.

    password_hash must already be bcrypt-hashed — this function does
    not hash passwords itself.
    Raises sqlite3.IntegrityError if username is already taken.
    """
    now = _now()
    with _tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (username, password_hash, role, now),
        )
        return cur.lastrowid


def update_user_password(user_id: int, new_hash: str) -> None:
    """
    Update the password hash for a user.

    new_hash must already be bcrypt-hashed — this function does not hash
    passwords itself. Raises ValueError if the user is not found.
    """
    with _tx() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, user_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"User not found: {user_id}")
    log.info(f"[db] Password updated for user id={user_id}")


def get_all_users() -> list[sqlite3.Row]:
    """Return all users (password_hash excluded in the API layer, not here)."""
    return _q("SELECT * FROM users ORDER BY created_at ASC")


def user_count() -> int:
    """Return the total number of users. Used for first-run bootstrap check."""
    row = _q1("SELECT COUNT(*) AS n FROM users")
    return row["n"] if row else 0


# ─── Sessions ─────────────────────────────────────────────────────────────────

def create_session(token: str, user_id: int, expiry_days: int = 30) -> None:
    """Insert a new session row."""
    now = _now()
    expires = now + expiry_days * 86400
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, user_id, now, expires),
        )


def get_session(token: str) -> Optional[sqlite3.Row]:
    """
    Return a valid (non-expired) session row, or None.

    Expired sessions are not automatically deleted here — call
    prune_sessions() periodically from the controller.
    """
    now = _now()
    return _q1(
        "SELECT * FROM sessions WHERE token = ? AND expires_at > ?",
        (token, now),
    )


def delete_session(token: str) -> None:
    """Delete a session (logout)."""
    with _tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def prune_sessions() -> int:
    """
    Delete all expired sessions. Returns the number of rows deleted.

    Call from a periodic background task (e.g. once per hour).
    """
    now = _now()
    with _tx() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        deleted = cur.rowcount
    if deleted:
        log.debug(f"[db] Pruned {deleted} expired session(s)")
    return deleted


# ─── System config ────────────────────────────────────────────────────────────

def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    """Return a system_config value by key, or default if not set."""
    row = _q1("SELECT value FROM system_config WHERE key = ?", (key,))
    if row is None:
        return default
    return row["value"]


def set_config(key: str, value: Optional[str]) -> None:
    """Insert or update a system_config key."""
    with _tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)",
            (key, value),
        )


def get_all_config() -> dict[str, Optional[str]]:
    """Return the full system_config as a plain dict."""
    rows = _q("SELECT key, value FROM system_config")
    return {row["key"]: row["value"] for row in rows}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> int:
    """Current time as a Unix timestamp (integer seconds)."""
    return int(time.time())
