# EchoMuse Controller — Backend & Management Console Spec

**Status:** Implemented (v2.2.0)  
**Last updated:** 2026-05-20

---

## Overview

The EchoMuse controller (`em_controller.py`) gains three additions:

1. **Persistence layer** — SQLite database for device registry, config, logs, and users
2. **HTTP/WebSocket API** — aiohttp web server running in the same asyncio event loop
3. **Management dashboard** — React SPA served by the controller, consuming the API

Everything runs in a single Python process. No additional services required.

---

## Device Identity

### Primary identifier: `ro.serialno`

The Go binary (`server`) reads the Android serial number at startup and sends it as `device_id` in the register message:

```go
out, _ := exec.Command("getprop", "ro.serialno").Output()
deviceID := strings.TrimSpace(string(out))
```

Serial numbers on Echo Dot Gen 2 are of the form `G0K0XXXXXXXX`. This is stable across reboots, unique per device, and matches the identifier shown by `adb devices`.

### Fields

| Field | Source | Description |
|-------|--------|-------------|
| `device_id` | `ro.serialno` on device | Primary key, immutable |
| `label` | Set by admin in dashboard | Human-friendly name e.g. "Kitchen" |
| `approved` | Set by admin | Whether device is allowed to connect |
| `config` | Stored in SQLite, pushed on connect | Per-device configuration |

---

## Connection & Approval Flow

### Approval modes

Controlled by `DEVICE_APPROVAL` environment variable:

| Mode | Value | Behaviour |
|------|-------|-----------|
| Strict (default) | `strict` | Unknown devices rejected, held in Pending queue — admin must approve |
| Auto | `auto` | Unknown devices approved immediately, labelled `Unknown {serial[:8]}` |

### Strict mode flow

```
Device boots
  → mDNS discovery → connects /control
  → sends {"type": "register", "device_id": "G0K0XXXXXXXX", ...}

Controller:
  → checks devices table for device_id

  CASE: approved = true
    → sends {"type": "ack", "device_id": "..."}
    → pushes stored config
    → normal operation

  CASE: row exists, approved = false (pending)
    → sends {"type": "pending"}
    → device: slow white LED pulse (1.5s cycle)
    → device retries connection every 30s

  CASE: no row (first seen)
    → inserts row: approved=false, label=null, first_seen=now
    → sends {"type": "pending"}
    → device: slow white LED pulse
    → appears in dashboard Pending section

Admin approves in dashboard:
  → sets approved=true, assigns label
  → next device connection attempt succeeds
```

### Auto mode flow

```
Unknown device connects
  → controller inserts row: approved=true, label="Unknown {serial[:8]}"
  → sends ack immediately
  → device appears in fleet, flagged "Needs label" in dashboard
```

### LED states for pending/rejected

| State | LED behaviour |
|-------|--------------|
| Pending (seen, not approved) | Slow white pulse — 1.5s cycle, all 12 LEDs |
| Disconnected / no server | Orange pulse (existing behaviour) |

The distinction matters — white means "server knows about me, waiting for approval", orange means "can't find server at all".

---

## Database Schema

SQLite, single file `echomuse.db` in the controller working directory.

### `devices`

```sql
CREATE TABLE devices (
    device_id     TEXT PRIMARY KEY,          -- ro.serialno
    label         TEXT,                      -- "Kitchen", "Bedroom", etc.
    approved      INTEGER NOT NULL DEFAULT 0,
    ip            TEXT,                      -- last seen IP
    firmware_ver  TEXT,                      -- EchoMuse binary version
    first_seen    INTEGER,                   -- unix timestamp
    last_seen     INTEGER,                   -- unix timestamp
    config        TEXT NOT NULL DEFAULT '{}' -- JSON blob
);
```

### `device_logs`

```sql
CREATE TABLE device_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT NOT NULL,
    ts         INTEGER NOT NULL,  -- unix timestamp ms
    level      TEXT NOT NULL,     -- info | warn | error
    source     TEXT NOT NULL,     -- device | controller
    message    TEXT NOT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);
CREATE INDEX idx_device_logs_device_ts ON device_logs(device_id, ts DESC);
```

Devices stream log lines to the controller over the control WebSocket (new `log` message type). Controller also writes its own per-device log entries (connect, disconnect, voice turn, config push etc.) with `source = controller`.

Log retention: keep last 10,000 entries per device. Prune on insert.

### `users`

```sql
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,              -- bcrypt
    role          TEXT NOT NULL DEFAULT 'readonly', -- admin | readonly
    created_at    INTEGER NOT NULL
);
```

### `sessions`

```sql
CREATE TABLE sessions (
    token      TEXT PRIMARY KEY,   -- random 32-byte hex
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

Simple token-based auth — no JWT, no expiry complexity. Token in `Authorization: Bearer <token>` header or `session` cookie. Sessions expire after 30 days (configurable).

### Schema versioning and migrations

A `schema_version` key in `system_config` tracks the current schema version. On startup, `db.py` reads the current version and runs any pending migrations in order before doing anything else.

Migrations are a simple ordered list of SQL strings in `db.py`. Adding a column, index, or table means appending one entry to the list — nothing else changes.

```python
MIGRATIONS = [
    # v1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id        TEXT PRIMARY KEY,
        label            TEXT,
        approved         INTEGER NOT NULL DEFAULT 0,
        ip               TEXT,
        firmware_ver     TEXT,
        firmware_previous TEXT,
        first_seen       INTEGER,
        last_seen        INTEGER,
        config           TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS device_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id  TEXT NOT NULL,
        ts         INTEGER NOT NULL,
        level      TEXT NOT NULL,
        source     TEXT NOT NULL,
        message    TEXT NOT NULL,
        FOREIGN KEY (device_id) REFERENCES devices(device_id)
    );
    CREATE INDEX IF NOT EXISTS idx_device_logs_device_ts
        ON device_logs(device_id, ts DESC);
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'readonly',
        created_at    INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS system_config (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    INSERT OR IGNORE INTO system_config VALUES ('schema_version', '1');
    INSERT OR IGNORE INTO system_config VALUES ('device_approval', 'strict');
    INSERT OR IGNORE INTO system_config VALUES ('session_expiry_days', '30');
    INSERT OR IGNORE INTO system_config VALUES ('update_check_interval', '3600');
    INSERT OR IGNORE INTO system_config VALUES ('github_repo', 'wilbowes/EchoMuse');
    INSERT OR IGNORE INTO system_config VALUES ('latest_version', NULL);
    INSERT OR IGNORE INTO system_config VALUES ('latest_binary_url', NULL);
    INSERT OR IGNORE INTO system_config VALUES ('last_update_check', NULL);
    """,

    # v2 — example future migration
    # """
    # ALTER TABLE devices ADD COLUMN some_new_column TEXT;
    # UPDATE system_config SET value = '2' WHERE key = 'schema_version';
    # """,
]

def migrate(conn):
    try:
        cur = conn.execute(
            "SELECT value FROM system_config WHERE key = 'schema_version'"
        )
        row = cur.fetchone()
        current = int(row[0]) if row else 0
    except Exception:
        current = 0  # system_config doesn't exist yet — fresh DB

    pending = MIGRATIONS[current:]
    if not pending:
        return

    for i, sql in enumerate(pending):
        version = current + i + 1
        conn.executescript(sql)
        # schema_version updated inside each migration's SQL
    conn.commit()
```

Rules:
- Migrations are **append-only** — never edit an existing entry
- Each migration is idempotent where possible (`CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE`)
- Each migration's SQL must update `system_config` `schema_version` as its last statement
- SQLite `ALTER TABLE` only supports `ADD COLUMN` — renaming or dropping columns requires a create-copy-drop migration
- If a migration fails the transaction is rolled back and the controller exits with a clear error — the database is never left in a partial state

---

## Default Config Schema

Per-device config stored as JSON in `devices.config`. On first approval, populated from server defaults. Pushed to device on connect as a `config` control message.

```json
{
  "adcDigitalGain": 100,
  "adcMicpga": 60,
  "startupVolume": 85,
  "vadThreshold": 0.004,
  "vadSpeechMs": 80,
  "vadSilenceMs": 600,
  "owwThreshold": 0.3,
  "owwModel": "hey_jarvis_v0.1"
}
```

Default values stored in `config.py` or env vars, used when a new device is approved without custom config.

---

## Control WebSocket Protocol — Additions

Two new message types added to the existing `/control` plane:

### Device → Controller: `log`

```json
{
  "type": "log",
  "level": "info",
  "message": "VAD gate opened"
}
```

Controller writes to `device_logs` with `source = device`, `ts = now`.

### Controller → Device: `config`

Sent immediately after `ack` on successful registration.

```json
{
  "type": "config",
  "adcDigitalGain": 100,
  "adcMicpga": 60,
  "startupVolume": 85,
  "vadThreshold": 0.004,
  "vadSpeechMs": 80,
  "vadSilenceMs": 600,
  "owwThreshold": 0.3,
  "owwModel": "hey_jarvis_v0.1"
}
```

Go side applies config values to tinymix and internal thresholds on receipt.

### Controller → Device: `pending`

```json
{"type": "pending"}
```

Device closes WebSocket, enters slow white pulse, retries after 30s.

---

## HTTP API

Served by aiohttp on the same port as the WebSocket server (path-based routing — `/api/*` goes to aiohttp, `/control` and `/data` go to the existing WebSocket handlers, `/` serves the dashboard SPA).

All `/api/*` endpoints require authentication except `/api/auth/login`.

### Error responses

All errors return a consistent JSON body regardless of which endpoint produced them:

```json
{
  "error": "Human-readable description",
  "code":  "machine_readable_snake_case"
}
```

HTTP status codes map to error categories:

| Status | Meaning | Example `code` |
|--------|---------|----------------|
| `400` | Bad request — missing or invalid fields | `missing_field`, `invalid_config` |
| `401` | Not authenticated | `not_authenticated` |
| `403` | Authenticated but insufficient role | `forbidden` |
| `404` | Resource not found | `device_not_found`, `no_rollback_available` |
| `409` | Conflict — action not valid in current state | `device_offline`, `update_in_progress` |
| `500` | Internal controller error | `internal_error` |

The `code` field is what the dashboard switches on for UI decisions (e.g. showing "Device is offline" vs "Update already in progress"). The `error` string is for humans and logs — its exact wording is not part of the API contract.

A helper in `api.py` produces these consistently:

```python
def error(code: str, message: str, status: int):
    return web.Response(
        status=status,
        content_type="application/json",
        body=json.dumps({"error": message, "code": code})
    )

# Usage
return error("device_not_found", f"No device with id {device_id}", 404)
```

### Auth

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth/login` | `{username, password}` → `{token, role}` |
| `POST` | `/api/auth/logout` | Invalidate session token |
| `GET` | `/api/auth/me` | Current user info |

### Devices

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/devices` | readonly | All devices — live state merged with DB |
| `GET` | `/api/devices/pending` | readonly | Unapproved devices |
| `POST` | `/api/devices/{id}/approve` | admin | Approve + set label, optionally set config |
| `DELETE` | `/api/devices/{id}` | admin | Remove device from registry |
| `PATCH` | `/api/devices/{id}` | admin | Update label |
| `GET` | `/api/devices/{id}/config` | readonly | Current config |
| `POST` | `/api/devices/{id}/config` | admin | Update + push config to live device |
| `POST` | `/api/devices/{id}/update` | admin | Deploy latest GitHub release to device |
| `POST` | `/api/devices/{id}/rollback` | admin | Roll back to `server.old` |
| `GET` | `/api/devices/{id}/logs` | readonly | Log history, supports `?limit=&before=` |

### Releases

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/releases/latest` | readonly | Latest version from GitHub, cached |
| `POST` | `/api/releases/check` | admin | Force re-poll GitHub releases API |
| `POST` | `/api/releases/deploy` | admin | Deploy latest to all connected devices |

### System

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/system/status` | readonly | Controller uptime, connected count, mode |
| `GET` | `/api/system/config` | admin | Server config (approval mode etc.) |
| `PATCH` | `/api/system/config` | admin | Update server config |

### Dashboard Shell WebSocket

| Path | Auth | Description |
|------|------|-------------|
| `WS /api/devices/{id}/shell` | admin | Proxies shell I/O to device via `/shell` WebSocket |

No ADB required. The Go binary exposes a `/shell` WebSocket endpoint. On connection it spawns `sh` (already running as root) and pipes stdin/stdout as raw binary frames. The controller proxies the dashboard's `/api/devices/{id}/shell` WebSocket transparently to the device's `/shell` WebSocket. The dashboard renders this in an xterm.js terminal.

The `/shell` connection is a third WebSocket on the device, separate from `/control` and `/data`, keeping shell traffic completely isolated from the audio pipeline.

### Live state WebSocket

| Path | Auth | Description |
|------|------|-------------|
| `WS /api/events` | readonly | Server-sent device state changes and log stream |

Dashboard connects once on load. Controller pushes JSON events:

```json
{"type": "device_update", "device_id": "...", "state": {...}}
{"type": "device_log", "device_id": "...", "entry": {...}}
{"type": "device_pending", "device_id": "...", "ip": "..."}
{"type": "device_connected", "device_id": "..."}
{"type": "device_disconnected", "device_id": "..."}
```

This replaces dashboard polling — state is pushed in real time.

---

## Auth Model

Two roles:

| Role | Permissions |
|------|-------------|
| `admin` | Full access — approve devices, push config, push binaries, shell access, manage users, change system config |
| `readonly` | View fleet state, view logs, view config — no mutations |

First-run bootstrap: if `users` table is empty, controller prints a one-time setup token to stdout on startup. Visiting `/setup` with that token lets you create the first admin account. After that, setup endpoint is disabled.

---

## OTA Binary Push

No ADB required. The entire update and rollback mechanism runs over the shell WebSocket connection and is self-contained on the device.

### Version embedding

The Go binary embeds its version string at build time:

```
-ldflags "-X main.Version=v2.1.0"
```

Version is included in the `register` message so the controller always knows exactly what's running:

```json
{"type": "register", "device_id": "G0K0XXXXXXXX", "version": "v2.1.0", ...}
```

The `devices` table tracks:

```sql
firmware_ver       TEXT,   -- currently running, from register message
firmware_previous  TEXT,   -- version of server.old if rollback available
```

### GitHub Actions build pipeline

Triggered on version tags (`v*`). Produces a release with the compiled ARM binary attached.

**Prerequisite:** GoTinyAlsa added as a git submodule so the Actions runner can fetch it:

```bash
git submodule add https://github.com/Binozo/GoTinyAlsa GoTinyAlsa
```

Update `compile.sh` and the Dockerfile to reference `./GoTinyAlsa` instead of `~/GoTinyAlsa`.

**`.github/workflows/release.yml`:**

```yaml
name: Build and Release

on:
  push:
    tags:
      - 'v*'

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true

      - name: Build compiler image
        run: docker build -t echomuse-compiler compiler/

      - name: Compile
        run: |
          docker run --rm \
            -e CGO_LDFLAGS="-Wl,--hash-style=both" \
            -e VERSION=${{ github.ref_name }} \
            -v "$(pwd)":/sdk \
            -v "$(pwd)/GoTinyAlsa":/GoTinyAlsa \
            echomuse-compiler

      - name: Release
        uses: softprops/action-gh-release@v1
        with:
          files: build/server
```

The Dockerfile passes `VERSION` through to the Go build as `-ldflags "-X main.Version=$VERSION"`. Tag a commit with `v2.1.0` → binary built for ARM/API 22 → attached to GitHub release as `server`.

### Controller release tracking

The controller polls the GitHub releases API hourly (background asyncio task) and caches the result:

```
GET https://api.github.com/repos/wilbowes/EchoMuse/releases/latest
```

No auth token required for a public repo. Unauthenticated rate limit is 60 requests/hour — one poll per hour uses one slot.

Response fields used:
- `tag_name` — version string e.g. `v2.1.0`
- `assets[0].browser_download_url` — direct download URL for the `server` binary

Cached in `system_config` table:

```sql
CREATE TABLE system_config (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
-- Populated on first run:
-- github_repo         wilbowes/EchoMuse
-- latest_version      null  (populated by poll task)
-- latest_binary_url   null  (populated by poll task)
-- last_update_check   null  (unix timestamp)
-- update_check_interval  3600  (seconds)
-- device_approval     strict
-- session_expiry_days 30
```

### Update flow

When admin clicks "Deploy v2.1.0" in the dashboard (either fleet-wide or per-device):

1. Controller fetches binary from `latest_binary_url` if not already cached locally
2. Streams binary to device at `/data/local/bin/server.new` over the shell WebSocket
3. Sets executable bit: `chmod 755 /data/local/bin/server.new`
4. Executes `update.sh` on the device over the shell connection

`update.sh` (pushed to `/data/local/bin/update.sh` during provisioning):

```bash
#!/system/bin/sh

# 1. Verify new binary exists
[ -f /data/local/bin/server.new ] || { echo "ERR: server.new not found"; exit 1; }

# 2. Back up current binary
cp /data/local/bin/server /data/local/bin/server.old

# 3. Stage new binary
cp /data/local/bin/server.new /data/local/bin/server
chmod 755 /data/local/bin/server
rm /data/local/bin/server.new

# 4. Restart service
stop echomuse
start echomuse

# 5. Wait up to 60s for service to be running
i=0
while [ $i -lt 60 ]; do
    getprop init.svc.echomuse | grep -q running && { echo "OK"; exit 0; }
    sleep 1
    i=$((i + 1))
done

# 6. Service not running after 60s — roll back
echo "ERR: service failed to start — rolling back"
stop echomuse
cp /data/local/bin/server.old /data/local/bin/server
start echomuse
exit 1
```

### Two independent health checks

The script checks whether the service is running on-device. The controller independently monitors whether a `register` message arrives from the device within 90 seconds and whether the reported version matches the pushed binary. Both must pass for the update to be considered confirmed:

| Outcome | Service running | Controller sees register | Result |
|---------|----------------|--------------------------|--------|
| Success | ✓ | ✓ new version | Update confirmed |
| On-device crash | ✗ | ✓ old version (after rollback) | Rolled back — flagged in dashboard |
| Connects but wrong version | ✓ | ✓ old version | Rollback detected — flagged in dashboard |
| No reconnection | — | ✗ | Alert admin — network/power issue |

### Manual rollback

While `server.old` exists on the device, the dashboard shows a "Roll back" button. Clicking it executes over the shell WebSocket:

```bash
stop echomuse
cp /data/local/bin/server.old /data/local/bin/server
start echomuse
```

Controller monitors reconnection and updates `firmware_ver` / `firmware_previous` accordingly.

---

## Go Binary — WebSocket Endpoints

The Go binary exposes three outbound WebSocket connections to the controller:

| Path | Direction | Content |
|------|-----------|---------|
| `/control` | bidirectional | JSON control plane — existing |
| `/data` | bidirectional | Binary audio frames — existing |
| `/shell` | bidirectional | Raw binary stdin/stdout — new |

`/shell` is demand-opened — the Go binary connects it only when the controller requests a shell session via a `shell_open` control message. On `shell_open`, Go connects `/shell` to the controller, spawns `sh`, and pipes stdio. On `shell_close` control message (or WebSocket close), the shell process is killed and the `/shell` connection closed.

---

### Current state
EchoMuse logs to `/tmp/server.log` on device (tmpfs — survives until reboot, not flash).

### Target state
EchoMuse sends structured log messages over the control WebSocket (`log` message type). Controller persists to `device_logs` table. `/tmp/server.log` kept as fallback for direct ADB debugging.

### Log levels
- `info` — normal operational events (connect, wake word, voice turn, config applied)
- `warn` — recoverable issues (queue full, mic timeout, resample underrun)
- `error` — failures (ALSA open failed, WebSocket error, OWW model load failed)

---

## File Structure

```
.github/
  workflows/
    release.yml           # build + release on version tag push — ✅ implemented
GoTinyAlsa/               # git submodule — Binozo/GoTinyAlsa — ✅ implemented
controller/
  em_controller.py        # WebSocket server, voice pipeline, OWW, shell router — ✅
  em_db.py                # SQLite layer — init, queries, migrations — ✅
  em_api.py               # aiohttp routes — REST API + dashboard WS — ✅
  em_auth.py              # session management, bcrypt, role checks — ✅
  Dockerfile              # builds image, vendors JS/fonts, compiles JSX — ✅
  docker-compose.yml      # ✅
  requirements.txt        # ✅
  .env.example            # ✅
  static/
    index.html            # placeholder page — ✅
    dashboard.html        # dashboard shell HTML — ✅
    dashboard.jsx         # dashboard React source — ✅
    dashboard.js          # compiled by esbuild at Docker build time (gitignored)
    vendor/               # vendored JS + fonts (gitignored, downloaded at build)
  data/
    echomuse.db           # created on first run, persisted via volume mount
```

---

## Implementation Order (completed)

1. ✅ **GoTinyAlsa submodule** — `git submodule add`, updated `compile.sh` and Dockerfile paths
2. ✅ **GitHub Actions workflow** — `.github/workflows/release.yml`, triggers on `v*` tags
3. ✅ **`em_db.py`** — schema init, device CRUD, log insert/query, user/session management, system_config
4. ✅ **`em_auth.py`** — login, token validation, role decorator
5. ✅ **`em_api.py`** — aiohttp app, routes, live events WebSocket, GitHub release poll task, shell coordination
6. ✅ **`em_controller.py`** — DB integration, config push, log/mute_state handling, shell routing, device state push events, WebSocket keepalives
7. ✅ **Dashboard** — React SPA compiled via esbuild at build time, vendored assets (no CDN), auth screen, live WebSocket events, pending approval, updates tab, shell terminal with Ctrl+C, device state (idle/listening/muted/offline)
8. ✅ **Go binary** — `ro.serialno` device ID, version via ldflags, log/pending/config/mute_state messages, shell_open/shell_close outbound dial (no inbound ports)

---

## Open Questions / Known Gaps

- **HTTPS**: Plain HTTP, acceptable for LAN-only. TLS via nginx/Caddy if externally exposed.
- **Config push at runtime**: Implemented — controller sends `config` message immediately if device connected. Go side applies tinymix changes on receipt without restart.
- **PTY shell**: Current shell is raw stdio — no terminal emulation. `top`, `vim` etc won't render correctly. Full fix requires `creack/pty` on Go side + xterm.js in dashboard.
- **Browser-based provisioner**: Not yet implemented. WebUSB/ya-webadb approach specced but deferred.
- **OTA binary transfer**: Currently uses base64-over-shell approach which is functional but slow for large binaries. Could be improved with a dedicated HTTP upload endpoint.

---

**Next step:** Implement `db.py` — schema, migrations, and CRUD layer.
