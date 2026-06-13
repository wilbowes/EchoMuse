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
    "startupVolume":    85,
    "vadThreshold":     0.004,
    "vadSpeechMs":      32,
    "vadSilenceMs":     600,
    "owwThreshold":     0.3,
    "owwModel":         "hey_jarvis_v0.1",
    "beamformingEnabled": True,
    "beamAngle":        -1,
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

    # ── v2 — example future migration (uncomment and adapt as needed) ────────
    # """
    # ALTER TABLE devices ADD COLUMN some_new_column TEXT;
    # UPDATE system_config SET value = '2' WHERE key = 'schema_version';
    # """,
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
