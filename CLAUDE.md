# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

EchoMuse repurposes Amazon Echo Dot Gen 2 (FireOS 5 / Android 5.1, codename "biscuit") as an open-source voice assistant satellite. Two components:

- **`device/`** — Go binary that runs directly on the rooted Echo Dot
- **`controller/`** — Python asyncio WebSocket server that manages devices, runs wake word detection, and proxies to a voice pipeline
- **`oww_forge/`** — standalone Docker batch trainer for custom openWakeWord models (synthetic TTS positives → augmentation → classifier head → `.onnx`). Not part of the controller; see `oww_forge/README.md`. Upstream pins in its Dockerfile are load-bearing (piper-sample-generator v2.0.0 flat layout; openWakeWord SHA with a `--convert_to_tflite` argparse patch). Models install via the dashboard (Config → Wake word → "+ Custom model" → `/api/oww_models/upload`) into `oww_models/` beside the SQLite DB; `owwModel` stores the file path for custom models. openwakeword keys predictions by filename *stem*, never the path — always score via `em_oww_models.prediction_key`

## Direction: portable, and not dependent on Amazon

**EchoMuse should run on more than one piece of hardware, with minimal change
per platform, and should not depend on Amazon's software to work.** That is
the direction, stated 2026-08-18. It is written here because contributors have
sent multi-thousand-line PRs without knowing which project they were
contributing to, and the answer changes how a change should be judged.

**The dependency is already thin, and keeping it thin is the job.** The entire
Android-specific surface in `device/` is about twenty call sites: `tinymix`
(×10), `stop <service>` (×6), `svc wifi` (×2), `getprop` (×2). Everything else
— mic, speaker, LEDs, buttons, ambient light, jack detect, WiFi state — is
ALSA, i2c, evdev, sysfs and wpa_supplicant. This is a Linux daemon that
happens to be running on Android because that is what shipped on the box.

Three consequences for reviewing a change:

- **Prefer the Linux interface to the Android one**, and where an Android call
  is unavoidable, isolate it rather than spread it.
- **Resolve hardware by NAME, not by number.** `event2` is the volume button
  on biscuit and the *touchscreen* on checkers; opening the wrong one succeeds
  silently and leaves the buttons dead. The same rule already applies to i2c
  (`als.resolve()` matches `tsl2540` by name, since `0-0039` is an
  enumeration accident).
- **A change that makes a vendor blob load-bearing is going the wrong way**,
  and needs to justify itself as a terminal opt-in for one platform rather
  than as the path forward. PR #168 (native AFE) is the live example.

**LineageOS is probably the wrong target; postmarketOS already has an
`amazon-biscuit` port** (its wiki and pmaports kernel config were corroborating
sources for the ALS second-source diagnosis — see JOURNAL 2026-08-11). There is
no Lineage port for a 2015 MT8163 on Android 5.1, and building one would mean
keeping the same MediaTek vendor blobs — swapping Amazon's Android for
somebody else's without removing the dependency.

The posture is therefore **not to own the OS work, but not to prevent it**:
keep `pkg/led`, `pkg/mic`, `pkg/speaker` and `pkg/buttons` honest as
interfaces, and treat each Android call site as something to isolate. Nothing
here commits the project to shipping a distro.

## Building the device binary

The Echo Dot runs FireOS 5 (API 22). Standard Go cross-compilation won't work — a custom Docker build environment is required.

**One-time setup:**
```bash
# GoTinyAlsa is a git submodule at the repo root — the wilbowes/GoTinyAlsa
# fork, NOT upstream Binozo: it carries the GetAudioStream defer-in-loop
# leak fix (v2.9.2). Don't repoint it upstream until that fix is merged there.
git submodule update --init

# Build the compiler Docker image (from device/)
cd device
docker build -t echomuse-compiler compiler/
```

**The compiler base is pinned by DIGEST, and must stay that way.**
`compiler/Dockerfile` carries the Go toolchain (1.24.0) and NDK
(21.4.7075529) that compile the firmware, so it is the layer sitting
directly on top of FireOS 5 — a 2015 platform that cannot be upgraded. It
was `FROM ghcr.io/binozo/echogo:latest`, a third party's floating tag, and
`release.yml` rebuilds the image **from scratch on every tag push**: every
release was free to pick up a different compiler than the last, with no PR
and no CI signal. The first symptom would be a binary the hardware refuses
to run, which is the one failure here not recoverable from the dashboard.

Moving the pin needs **a real device in the loop**. The host tests and
`go vet` cannot speak to it — they run on amd64 with the host toolchain,
and this image is exercised only by `compile.sh` and `release.yml`, so a
green CI run on a pin change proves nothing about it.

**Compile:**
```bash
cd device
./compile.sh
# Output: build/server
```

`compile.sh` embeds the git version string via `-ldflags "-X .../client.Version=..."`. Dirty trees get a `YYYYMMDD-HHMM-dev` timestamp instead of the tag.

**Run Go tests (host):**
```bash
cd device
go test ./...
```

Tests only cover pure-Go logic — hardware-dependent code is not testable on the host.

**Run controller tests (host):**
```bash
cd controller
python -m pytest tests/        # needs: pytest numpy scipy pyyaml — not the full requirements.txt
```

Controller tests cover the pure-logic modules only (`em_eq`, `em_scenes`, `em_oww_models`, `version`, `em_hostip`, `em_ingressauth`) — keep it that way unless you're prepared to pull openwakeword/aiohttp into the test environment. Both suites (plus `go vet`) run in CI on every push/PR (`.github/workflows/ci.yml`).

**Release:** pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds the binary in the compiler image and attaches it to a GitHub release. **Tag with `git tag -a --cleanup=verbatim`** — the annotation message becomes the release body (`body_path` from `git tag -l --format='%(contents)'`), which is what the dashboard shows next to an available update. Write it for the person deciding whether to push firmware to a device they depend on: what changed, what to expect, anything required of them. GitHub's generated commit list is still appended below it. A lightweight tag yields an empty body and falls back to that list, which is a worse experience, not a broken one.

**`--cleanup=verbatim` is not optional if the notes use Markdown headings.**
`git tag -a` defaults to `--cleanup=strip`, which treats a line beginning with
`#` as a comment and deletes **the whole line** — so `## Volume` does not lose
its markers, it disappears entirely. v2.12.0 shipped that way: the notes were
structurally correct in the file, five headings gone from the published body,
and the only visible sign was a wall of paragraphs. Fixing it afterwards means
`gh api -X PATCH repos/<owner>/<repo>/releases/<id> -F body=@notes.md`
(`gh release edit` has no `--notes-file`), and re-appending GitHub's generated
commit list by hand, since the PATCH replaces the whole body.

**Device/controller compatibility.** The two halves version independently, so any pairing can occur in the field. Two rules, both guarded by `tests/test_capabilities.py`:
- **Negotiate by capability, not version.** The device announces what it implements in its register message (`internal/client/control.go`: `mic`, `speaker`, `leds`, `led_anim`, `buttons`, `oww_shadow`, `button_hold`, and `ambient_light` **only when the sensor is actually readable**); the controller reads `Device.capabilities` via properties like `led_anim_capable` / `oww_shadow_capable`. Never compare version strings — that puts release history in the controller and misjudges dev builds. A UI control whose feature the device lacks is shown **disabled with the reason**, never as a control that silently does nothing.
- **Degrade to old behaviour, never to a wrong answer.** Unknown JSON fields and message types are ignored both ways. Where a new field records a measurement, absence stores as **NULL, not 0** — old firmware reporting no `playback_stats` must not read as "zero underruns", and a device that cannot score wake words locally must not read as "scored and missed" (hence `turns.dev_shadow` alongside `dev_wake_score`).

### Schema migrations

`em_db.MIGRATIONS` is **append-only** — the stored `schema_version` is an
index into it, so appending to a deployed entry corrupts every database that
already ran it. (Doing exactly that once broke every stats write and
disconnect-looped the fleet.)

A controller applies everything it is missing in one startup, so **a user
several releases behind jumping straight to latest is the normal case**, not
an exotic one — verified end to end from v11 to v16 with data intact. Each
migration is its own transaction including its Python fixup, and a failure
refuses startup rather than running on a half-migrated schema; re-running
resumes from the last committed version.

Two guards sit in front of that, both tested by reintroducing the bug:
- **A backup is taken before migrating** (`<db>.pre-v<N>.bak`, via sqlite3's
  backup API so it is consistent and WAL-correct). Named for the version
  being left, so a retry overwrites rather than accumulating; skipped for a
  fresh database and for an ordinary restart. A backup that cannot be written
  **refuses to migrate** — disk-full is precisely when the schema should be
  left alone — with `EM_SKIP_DB_BACKUP=1` as the escape hatch.
- **A newer database refuses to start.** `MIGRATIONS[current:]` is empty when
  the DB is ahead, so an older controller used to start silently against a
  schema it did not know. It mostly works, since the newer schema is a
  superset — and "mostly" is the problem, because the failure then surfaces
  as odd behaviour elsewhere, exactly when someone has rolled an image back
  and is already troubleshooting.

## Versioning / releases

Device firmware and controller are versioned independently from the same repo:

- **Device**: plain `v*` tags (e.g. `v2.7.6`) → `release.yml` → GitHub Release with the `server` binary asset. The tag is embedded in the binary and compared against `firmware_ver` by OTA — don't change this scheme.
- **Controller**: `controller-v*` tags (e.g. `controller-v2.8.0`) → `controller-release.yml` → Docker image pushed to `ghcr.io/wilbowes/echomuse-controller` (`X.Y.Z` + `latest`, CPU-only, **multi-arch: linux/amd64 + linux/arm64** — it said amd64 here until 2026-08-13, long after arm64 shipped). **No GitHub Release is created** — the OTA system's release polling (`em_api._fetch_latest_release`) filters for `v*` tags with a `server` asset, but controller releases stay out of the releases list entirely by design. **Tag controller releases with `git tag -a --cleanup=verbatim` too**: with no Release behind them, the annotation is the *only* copy of the notes, and it is what the dashboard's controller-update notice displays (`em_api._fetch_controller_release` reads it via `git/matching-refs` + the tag object). A lightweight controller tag ships an image nobody can read a changelog for. Pick the newest tag by **parsed version, never list order** — the refs API sorts lexically and returns `controller-v2.9.0` *after* `controller-v2.10.0`.

  The notice is **advisory only and must stay that way** (`tests/test_deploy.py` enforces GET-only + no mutating call in the banner): the controller is the user's container, updated with their own `docker compose pull`. An in-app update would restart the process serving the page, mid-request, with no way to report the outcome. Note a locally-built image defaults `EM_CONTROLLER_VERSION` to `dev`, which resolves to `unknown` and correctly shows nothing — pass `--build-arg EM_CONTROLLER_VERSION=$(git describe --tags --match 'controller-v*')` for a local build that knows what it is. Version comparison lives in `version.py` (`parse`/`compare`) so it is unit-testable without aiohttp; a build between tags parses **equal** to its tag and is ahead, not behind.

The controller's own version is resolved by `controller/version.py` (env `EM_CONTROLLER_VERSION` — baked into the image from the tag — then `git describe --match 'controller-v*'`, then `"dev"`). It's exposed at `/api/system/status` as `controller_version`, shown in the dashboard header, and reported to HA as the ESPHome project version.

`controller/docker-compose.yml` is the local dev/GPU build (`GPU=1` build arg swaps in onnxruntime-gpu); `controller/docker-compose.deploy.yml` is the user-facing compose that pulls the published image.

`device/tools/` contains standalone diagnostics (`capture_mics`, `bf_capture` + analysis scripts) for mapping the 9-channel mic array; they build inside the same compiler image.

## Running the controller

**Bare metal (Python 3.12):**
```bash
cd controller
cp .env.example .env   # fill in SERVER_IP
pip install -r requirements.txt
python em_controller.py
```

**Docker:**
```bash
cd controller
docker-compose up --build
```

Dashboard available at `http://<SERVER_IP>:8768`. WebSocket devices connect to port 8767.

Key env vars in `.env` (see `.env.example` for the full list):
- `SERVER_IP` — LAN IP advertised via mDNS (devices connect here)
- `OWW_MODEL` / `OWW_THRESHOLD` — OpenWakeWord model name and detection threshold
- `DEVICE_APPROVAL` — `strict` (admin must approve new devices) or `auto`
- `DEBUG` — `1` raises the controller to DEBUG. Read **once at import**, so
  a change needs a restart, and parsed as `== "1"` rather than a bare
  truthiness test: every non-empty string is truthy in Python including the
  `"0"` that `em_start.py` writes for a false add-on option, which would put
  every add-on install at DEBUG with the toggle showing off. It is also an
  add-on option (`debug`), because it went unreachable there until
  2026-08-16 while being the first thing support asks for — the #163 class
  of gap. `tests/test_deploy.py` walks all four things an option needs (a
  default, a schema type, a translation, an `OPTION_ENV_VARS` line) in both
  directions; the missing env mapping is the quiet one, since Supervisor
  accepts, displays and stores the setting and `em_start` warns to a log
  nobody reads.

### Home Assistant add-on

The controller also ships as a Home Assistant Supervisor add-on
(`controller/config.yaml` + `repository.yaml` at the repo root, from #122).
Supervisor clones the **default branch**, so add-on files only take effect
once they are on `main` — a branch cannot be installed.

**The add-on and the standalone container are both first-class, and neither
may gain or lose capability relative to the other.** A setting reachable in
one and not the other is a divergence that silently invalidates
documentation and support answers depending on how someone installed. Every
`.env` variable therefore needs a matching add-on option (`options` +
`schema` + `translations/en.yaml`, plus `em_start.py`'s `OPTION_ENV_VARS`)
or a stated reason it is fixed. Two are deliberately fixed: `DB_PATH`
(pinned to `/data/…` so it cannot point somewhere that does not survive a
restart) and `API_PORT` (must equal `ingress_port`). `SERVER_TLS_PORT` and
`SERVER_PORT` are unreconciled gaps — #163.

**`config.yaml`'s `version:` pins the image Supervisor pulls**, so it names
an artifact that must exist AND must contain the add-on code. Shipping it
pinned to `2.18.0` — an image built before the add-on existed — started a
controller with no ingress support at all, which presented as two unrelated
faults: the dashboard answering the LAN with 200 instead of 403, and the
panel throwing `JSON.parse ... column 4` because the old bundle's absolute
`/api` paths reached Home Assistant, which answers `404: Not Found` as plain
text. `controller-release.yml` now refuses to build when the tag and the pin
disagree; there is nothing else that catches it, since it fails no test and
fails no release.

**Ingress.** `em_start.py` bridges `/data/options.json` into the env vars
`em_controller.py` already reads and execs into it, so the controller stays
unaware of Home Assistant. `em_api` injects a `<base href>` from the
`X-Ingress-Path` header and `dashboard.jsx` routes every fetch through
`ingressPath()`; absolute paths bypass the base and hit HA instead.
`_ingress_only_middleware` rejects anything whose `request.remote` is not
Supervisor's gateway `172.30.32.2` — **verified on hardware 2026-08-13**,
including under `host_network: true`, where the container shares the host
netns and the assumption looked shaky. That gate is the add-on's only
network protection, since host networking exposes 8768 on the LAN.
`/api/system/status` reports `ha_ingress` for presentation only: the
dashboard does provisioning, OTA, the shell and turn history, none of which
HA offers, so **nothing is gated on it**.

**Auth under ingress: Home Assistant has already done it.** Supervisor
forwards the authenticated user as `X-Remote-User-Id` (plus optional name
headers) and **strips any client-supplied copies** before proxying, so on a
genuine ingress request those values are proof of an HA session. `POST
/api/auth/ingress` mints an EchoMuse session from them and the landing page
tries it before rendering any form, which also removes the bootstrap-token
step under the add-on — the first HA user through the door becomes admin,
exactly as the token holder does on the container.

The decision is `em_ingressauth.decide`, pure and tested, for the reason
`em_linkauth.decide` is: **two conditions, never one.** The header only means
something when `INGRESS_ONLY` **and** the peer is Supervisor's gateway. On the
standalone container that same header is attacker-supplied, so honouring it
there is an unauthenticated admin session on a dashboard that proxies a root
shell. `tests/test_deploy.py` pins that the call site passes the live
`INGRESS_ONLY` and `request.remote` rather than literals, and that nothing
else in the tree reads those headers.

Users are keyed on the **HA user id, never the name** (schema v18,
`users.ha_user_id`) — names are editable in HA, and keying on one would hand a
renamed user a fresh account, or hand somebody else's account to whoever took
the old name. These rows carry a password sentinel that no bcrypt check can
match, so they can never be used on the password form.

**Roles are NOT mirrored from Home Assistant.** The first user through the
door is admin; everyone after is read-only until promoted via
`PATCH /api/users/{id}` (admin, and it refuses to demote the last admin —
on the standalone container local accounts are the only auth, so there would
be no way back in). Nothing overwrites a role on a later login, which is what
makes a promotion stick.

Mirroring was built and then removed on 2026-08-14, deliberately. Supervisor
forwards **no admin flag** — only the user id and names — and HA core's
ingress view sets `requires_auth=False` and leans on the session token, so
`panel_admin` hides the sidebar entry rather than gating the URL: **reaching
this dashboard is not evidence of being an HA admin.** Asking HA therefore
needs a permission, and both routes are too expensive for one boolean.
`homeassistant_api` grants the entire HA API; `auth_api` looks narrow but its
`/auth` set includes **`POST /auth/reset`, which sets any Home Assistant
user's password with no verification**. On a single-operator system that is a
steep price for automation the `PATCH` endpoint already covers. Revisit under
#171 if real multi-user demand appears.

**Recordings and transcripts are admin-only** — `_get_turn_audio` is
`require_admin` and `_redact_turns_for` strips `stt_text` from `/turns` for
non-admin sessions. This is recognisable speech from inside someone's home,
and once every household HA user can reach the dashboard, "read-only" stopped
meaning "trusted with the recordings". Enforced **server-side**: `/turns` is a
plain GET with a session token, so a dashboard-only rule protects nothing from
anyone who opens the network tab. `tests/test_deploy.py` pins both.

There is no **Sign out** control on a session obtained through ingress: HA owns
it, so signing out would re-authenticate immediately and read as a broken
button. Keyed on how the session was obtained (`em_auth_via`), not on whether
the page is under ingress — those differ when Supervisor forwards no user and
the person falls back to the password form, and they can sign out.

Note HA display names contain spaces, unlike every local username, and they
now reach the user table — `em_support`'s account redaction already covers
them because it reads that table rather than matching a pattern, and a test
pins it.

**Release channels.** Home Assistant has no channel concept — one add-on is
one version — so a channel is a **second add-on with its own slug**, opted
into by installing it (the shape ESPHome uses). `controller/` is GA;
`controller-ea/` is Early Access and is **generated** by
`controller/tools/sync_channels.py`, never edited: everything but the add-on's
identity and its own `version:` is copied verbatim, and
`tests/test_channels.py` fails on drift (verified by reintroducing it). Two
hand-maintained config files describing one program is #160's failure
multiplied by every option, schema entry and permission.

One publish path, channel taken from the tag: `controller-v2.20.0` →
`:2.20.0` + `:latest`; `controller-ea-v2.20.0-ea.1` → `:2.20.0-ea.1` only.
**EA never moves `:latest`** — that is what `docker-compose.deploy.yml` pulls,
so an EA build touching it would push a prerelease to every standalone
container on the next `docker compose pull`. The prefixes are distinct rather
than one scheme with a suffix because `_fetch_controller_release` lists tags
by **prefix match** on `controller-v`, which excludes `controller-ea-v*` from
the dashboard's update notice for free.

**Channels share no storage.** Each slug gets its own `/data`, so switching is
a migration: a new database *and a newly generated CA*, and devices holding the
old CA then dial `wss`, fail verification and cannot connect (they take `wss`
from the mDNS `tls_port` record, so `require_device_tls: false` does not help).
Copy the old `/data` — at minimum all four files in `tls/` — before starting
the other channel. Isolation is deliberate: `MIGRATIONS` is append-only and
forward-only, so a shared database would let EA upgrade the schema out from
under GA, permanently.

**Because they share no storage, they must not share satellite PORT ranges
either.** Each channel has its own port counter, so both would allocate from
16001 and hand the same numbers to different devices — and Home Assistant keys
an ESPHome config entry on host and port, so after a switch its stored entries
reach whichever device now holds that number. Measured 2026-08-19: every
satellite entity unavailable for a day, the wake word still firing and the ring
still lighting, every turn dying in milliseconds because no HA pipeline was
behind it. It reads as a wake-word regression, and it is not one.
`EM_ESPHOME_PORT_BASE` (add-on option `esphome_port_base`) separates them — GA
16001, EA 16101, BLE proxies derived at +`BLE_PORT_OFFSET` so 17001/17101
follow with no second setting. It is applied as a **floor at allocation time,
never a seed**: the counter only moves forwards, so a base can never land on a
port a device already holds, a fresh database starts at the base, and an
established one jumps at its next allocation leaving every fielded device
alone. `sync_channels.py` owns EA's value as channel identity, and
`test_channels.py` fails if the two ever collide or come within 100 ports.
This bounds the damage of a switch; it does **not** make channels
interchangeable — the CA and database still have to be copied.

**`version.parse` does not order prereleases** — `2.20.0-ea.1` parses equal to
`2.20.0`, so the dashboard's update notice cannot tell an EA build from the GA
release of the same version. Advisory-only and Supervisor drives real updates,
so it is a wrong banner rather than a wrong install. Fixing it needs care: git
describe output (`2.19.0-3-gabc`) must keep parsing **equal** to its tag, so
a real prerelease has to be distinguished from a describe suffix.

**Testing add-on changes before they land.** Supervisor clones the **default
branch**, so a branch cannot be installed. `controller/tools/make_dev_addon.sh`
packages the working tree as a **local** add-on (dropped `image:`, distinct
slug) that Supervisor builds from `/addons/<folder>` on the HA host. The build
compiles nothing — ffmpeg is apt, onnxruntime/speexdsp-ns/scipy/scikit-learn
are prebuilt wheels — so it costs minutes.

**Migrating an existing fleet is not just a config change.** The device
picks `wss` from the mDNS `tls_port` TXT record, NOT from
`REQUIRE_DEVICE_TLS` (`internal/client/control.go`) — so a device holding
an old CA meeting a controller with a freshly generated one dials wss,
fails verification and cannot connect, and `require_device_tls: false` does
not help. Copy the old `data/tls/` (all four files — the server cert must
match the CA) into the add-on's `/data/tls/`. The devices then verify,
arrive at a controller whose DB does not know them, and are allowed through
because `em_linkauth` **ignores** a token for a device with nothing on
record; they appear as pending and are approved onto fresh config.

### Voice backend

The controller impersonates ESPHome voice satellites: one asyncio TCP listener per device on ports 16001+ (persisted in the device registry, never reused). Home Assistant's built-in ESPHome integration dials in and drives voice turns via Assist. Implemented in `em_esphome.py` on top of the protocol layer in `controller/esphome/` (`frame_protocol.py`, `satellite_server.py`, vendored aioesphomeapi protobufs in `esphome/vendor/`). Servers are created at startup for every approved device **and on demand** when a device approved after boot first connects (`_register_device_server` — idempotent on purpose: the startup loop and `device_connected()` race, first creation wins). HA naming: friendly name is `<label> Voice Assistant` (BT proxy: `<label> BT Proxy`); `project_name` carries `ESPHOME_DEVICE_MODEL` after the dot because HA displays that segment as the device Model, overriding DeviceInfo's `model` field. (A legacy `claracore` WebSocket backend was removed 2026-07-12 — ESPHome/HA is the only voice path.)

**Entity names must NOT repeat the device label.** HA sets
`_attr_has_entity_name = True` for every esphome entity and composes
`<device name> <entity name>` itself, and our device name is already
`<label> Voice Assistant` — so a label in the entity name renders twice
("Lounge Voice Assistant Lounge"). It did, on every device and every entity,
until 2026-08-16. The media player takes an **empty** name, which is HA's
convention for a device's primary entity (`self._attr_name =
static_info.name or None`) and renders as the device name alone.
`tests/test_deploy.py` pins that no `ListEntities` name references
`self.label`. Note fixing this changes only the displayed name: entity keys
are untouched, so registry rows and **entity_ids survive** and automations
keep working.

**`wake_word_phrase` is not optional.** The pipeline start must name which
wake word fired, and the string must be **identical** to the `wake_word` we
advertise — HA matches it against the STATE of its wake-word select entity
(`ww_state.state == wake_word_phrase`), whose options are those display
names. Both therefore come from `em_oww_models.display_name`, one function,
pinned by test. Send `""` for a button turn: aioesphomeapi maps empty back to
`None`, which is how the protocol says "no wake word", and claiming one would
be untrue. Sending nothing at all is what stalled HA's **voice satellite
setup dialog** for the life of the feature — it arms an interceptor for the
next wake word, and on `None` raises
`AssistSatelliteError("No wake word phrase provided")` and ends the run in
milliseconds. Every ordinary turn worked, so only the one flow that asks a
device to prove it heard a wake word ever noticed.

**A `RUN_END` with no preceding `RUN_START` is terminal; one after it is
not.** HA's interception path emits `RUN_END` and returns without ever
starting a pipeline, while a genuine run is `RUN_START` (measured 2ms before
`STT_START`) … `RUN_END` (last, after `TTS_END`). That is the discriminator —
structural, not a timing race — and it matters because HA *does* emit a
premature `RUN_END` mid-turn, which must stay non-terminal or genuine turns
get cut short. Without it the turn held the mic until our own timers expired
(20s streaming cap, 5s no-speech) while HA re-armed 18ms later. The outcome
is `pipeline_refused`, never `no_speech` — the audio was captured and
streamed into a closed run, and `no_speech` is persisted, so it would put
every HA-side refusal into the activity stats as a silent user.
Note this makes turns end in **milliseconds**, so the ring needs an explicit
`ack_anim` cue or it flashes and reads as a glitch; it previously stayed lit
only because the turn was hung.

**The protocol has NO run identifier, and HA does not serialise runs.**
`VoiceAssistantEventResponse` is an event type plus a name/value list and
nothing else, so a client structurally cannot attribute an event to a run: the
protocol assumes one pipeline run at a time per connection and **the satellite
is what enforces that**. HA does not — `handle_pipeline_start` clears the audio
queue and cancels `_tts_streaming_task`, then overwrites `_pipeline_task`
*without cancelling the old one*, so a second start orphans the first run and
leaves it emitting onto the same socket.

Barge-in is the only place two runs overlap, and it was broken for the life of
the feature: measured 2026-08-17, five barge-ins, five interrupting turns dead
in 4-17ms with zero audio, because the aborted run's `RUN_END` landed ~4ms
after the new turn started and the branch above read it as terminal. Two
halves, both required:

- **`VoiceAssistantRequest(start=False)` IS a server-side abort** —
  aioesphomeapi maps it to `handle_stop(True)` → HA's `_abort_pipeline()`,
  which queues the audio sentinel AND cancels `_pipeline_task`. `cancel_turn`
  used to claim the protocol had no such mechanism, citing an
  `ESPHOME_SPEC.md §7.4` that is not in this tree. It has one; we never sent
  it. (`VoiceAssistantAudio(end=True)` → `handle_stop(False)` is the *graceful*
  end, which is what VAD end already sends.)
- **HA acknowledges an abort with no wire message at all**, so the barrier is
  ordering, never a timeout: after an abort, discard every event until the next
  `RUN_START`, which is necessarily ours. `em_runbarrier` holds that state.
  Armed **only** by an abort and bounded to **one turn**, so the
  `_ha_never_started` path above — a genuine `RUN_END` with no `RUN_START`,
  which stalled the satellite setup dialog when it was missed — stays
  untouched. `RUN_START` releases the barrier and is itself **delivered**, not
  swallowed; eating it would leave `_run_started` False and re-arm the same bug
  for the turn's own terminal `RUN_END`.

**Announcements: HA has TWO paths and only one waits for a reply.**
`VoiceAssistantAnnounceRequest` blocks —
`assist_satellite.entity.async_internal_announce` holds `_is_announcing` and
the RESPONDING state for the duration and raises `SatelliteBusyError` on a
concurrent announce, and the esphome side awaits
`send_voice_assistant_announcement_await_response`. `play_media` with
`announce=true` is an ordinary media_player command and waits for nothing;
sending `AnnounceFinished` there answers a question nobody asked. Both resolve
their playback callback through **one** `_announce_play_cb`, because renaming
the old shared helper updated one call site and not the other and shipped an
`AttributeError` on every `play_media` announce (2.20.1-ea.1).

So `AnnounceFinished` is sent when playback **actually finishes**, from a
`finally` on every path, and `success` reports whether the audio reached the
speaker. Answering early returns the service call while audio is still playing,
drops the entity out of RESPONDING, and lets chained announcements overlap. Not
answering at all is worse: it parks HA for `_ANNOUNCEMENT_TIMEOUT_SEC`
(**5 minutes**) holding `_is_announcing`, after which every announcement fails.
The old code answered synchronously and justified it as stopping the setup
wizard timing out — it would not have: the wizard's connection test does not
wait on this message, it fires when the device fetches
`CONNECTION_TEST_URL_BASE`.

**`cancel_event` must be cleared by anything that starts playing, not just a
voice turn.** It is set by a cancel (a button press mid-turn, a mute) and was
cleared *only* at voice-turn start, so a cancelled turn silently killed every
subsequent **announcement** — `_run_post_turn_playback` checks the flag.
Measured on Test Device 01: a turn cancelled at 12:02:32 left seven
announcements over three minutes logging `Cancelled during playback` and
playing nothing. With two devices it reads as a routing fault, because the
other device is fine. An announcement is a new action and nothing that set that
flag earlier has a claim on it.

**`VoiceAssistantSetConfiguration` is handled but not applied.** It is HA
writing a wake-word choice back to us. We advertise one model with
`max_active_wake_words=1`, so the dropdown offers our model plus "no wake
word" and there is nothing to switch between; an empty list means "deafen
this satellite", which is a real request we do not implement and log at
warning rather than drop. Applying it, and offering a choice worth making,
both wait on #112.

### HA entities beyond the voice satellite

Both are advertised **only when the device declares the capability** — an
entity whose events can never fire, or whose state is permanently missing, is
worse than no entity, because someone writes an automation against it and it
silently never runs. Entity keys are per-device and **append-only**: HA keys
its registry on them, so renumbering renames everyone's entities.

**The entity list is a ONE-SHOT at `ListEntities`, so a capability that
arrives late is lost for the life of the HA connection** — and HA does not
reconnect on its own. There are two ways to arrive late and they need
different fixes. Arriving before the server exists is `_pending_caps`, below.
Arriving after HA has already enumerated is `set_device_capabilities`
bouncing the HA connection so it redials and re-reads, the same remedy
`update_oww_model` uses for the wake word configuration. **Gate that bounce on
the set actually changing**, or HA is disconnected on every device reconnect.
It is not a theoretical path: `als.resolve()` deliberately does not cache a
negative result, so a device can register without `ambient_light` and acquire
it on a later scan. A device **registers before its ESPHome server exists**
(the listener only comes up once the device is present), so
`set_device_capabilities` used to find no server and silently do nothing;
`_pending_caps` holds them until there is a server to take them, and the
server seeds from it at creation. Being a race it resolved differently on
every controller restart, which is why an ambient-light sensor came and went
rather than never working, and why it survived so long (measured on Retreat:
registered 05:25:33, server created 05:25:34). Never assume ordering between
registration and server creation — `tests/test_capabilities.py` pins both
directions.

- **Action Button** (`ListEntitiesEventResponse` / `EventResponse`, `button_hold`)
  — a hold fires `long`. Hold time is measured **on the device** (`heldMs`,
  reported on release), never by timing the down/up messages controller-side:
  RTT excursions past 1600ms have been measured on this fleet, which would
  turn a 750ms gesture into noise. Absent `heldMs` reads as a tap, so old
  firmware keeps its existing behaviour. A hold always fires `long`; a tap
  fires `single` only under `buttonSingleTapEvent`, which makes the tap an HA
  event rather than a voice turn. `double`/`triple` need `buttonMultiTapMs`
  as well, since knowing a press was *single* means delaying it by the
  multi-tap window — a cost worth paying only once a tap is an event and not
  speech.
  **`buttonSingleTapEvent` is gated on `button_hold`** — the event entity is
  only advertised for a hold-capable device, so on older firmware the setting
  is refused rather than leaving the button inert.
  **Multi-tap repeats the mistake `heldMs` exists to avoid** (#115). The hold
  is timed on the device; the multi-tap window is timed at the CONTROLLER, on
  arrival, so the gap it measures is the real gap plus the RTT difference
  between the two taps. Measured on Test Device: 2566 probes, **26.4% over
  200ms**, min 1ms, max 9255ms — so a genuine 120ms double-tap routinely
  arrives >150ms apart and reads as two singles, and a slow-then-fast pair can
  merge taps that were never a burst. `buttonMultiTapMs: 350` is reliable
  here for double and triple and 150 is not, but that is a value that clears
  today's jitter, not one derived from anything. The fix is for the device to
  report the gap since the previous tap from its own monotonic clock; the
  expiry timer can stay controller-side, since it only answers "has the burst
  stopped", where being late costs nothing. **Do not rewrite the coalescer** —
  per-tap window restart, `enabled()` re-checked at expiry rather than at tap
  time, and count reset before emit are all correct.
  **Mute blocks the voice TURN, not the gesture.** A hold fires `long` while
  muted; only the tap-starts-a-turn path is refused (`em_button.decide`, with
  the mute state read off the press itself — the device sends `muted` on every
  button event). The device used to drop every dot press while muted, which
  was right while the button meant one thing and became wrong the day a hold
  started firing an HA event: a hold bound to something unrelated to speech
  stopped working whenever the mic was off, with nothing on the device
  connecting the two. It shipped that way in v2.10.0 and no test noticed,
  which is why the decision is now a pure function with one.
  **Moving that check controller-side does not weaken mute**: sovereignty is
  the device rejecting every `mic_start` while muted plus the hardware ADC
  mute, not the button filter. A controller with a stale mute view can at
  worst start a turn that captures silence and ends `no_speech` — keep that
  rejection in `cmd/server.go` where it is, it is what makes this safe.
  A tap under `buttonSingleTapEvent` fires while muted too, for the hold's
  reason: it is an event, not speech.
  **The evdev reader must filter to `EV_KEY`**: every press is followed by an
  `EV_SYN` whose code and value are both 0, and without the filter that SYN
  read as a release microseconds after the press. The button therefore acted
  on the SYN rather than the real release for its entire history — invisible
  until something needed to know how long it was held.
- **Ambient Light** (`ListEntitiesSensorResponse` / `SensorStateResponse`,
  `ambient_light`) — lux. A device with no sensor sends `missing_state`, not
  0, because 0 lux is a real reading.

## Architecture

### Device → Controller protocol

Each device opens **three** WebSocket connections to the controller:

| Path | Direction | Purpose |
|------|-----------|---------|
| `/control` | bidirectional JSON | Registration, LEDs, mic_start/stop, button events, config push |
| `/data` | binary | Mic PCM frames in (0x01 header), speaker PCM frames out (0x02/0x03) |
| `/shell/{device_id}` | raw binary | Root shell proxy (demand-opened by device on `shell_open` command) |

Controller is discovered by the device via mDNS (`_emcontroller._tcp.local`).

### Device-link TLS + token auth

All three WS planes exist twice: plain on `SERVER_PORT` (8767) and TLS on `SERVER_TLS_PORT` (8770, `wss://`). `em_pki.py` generates a private CA + server cert on first start (persisted in `tls/` next to the SQLite DB; delete the dir to rotate — every device then needs a fresh credential push). The leaf's identity is the fixed DNS SAN `echomuse-controller` (`TLS_SERVER_NAME`, coupled with `tlsServerName` in `device/internal/client/tlscreds.go`) — never an IP, so the controller can move address freely. Certs are backdated 10y/valid 25y **and** the device clamps its verification clock to the firmware build time (`BuildUnix` ldflag): Echos boot with bogus clocks pre-NTP, and a device that can't connect can't fix its clock. Don't "normalise" either half of that.

Device behaviour (`tlscreds.go`): credentials live at `/data/local/etc/echomuse/{ca.pem,token}` (canonical path constant: `em_api.DEVICE_TLS_DIR`) and are **re-read on every dial**, so a push takes effect on the next reconnect, no restart. CA present + `tls_port` mDNS TXT property → dial wss; CA present but no TXT → plain with a warning (deliberate rollout fallback). The token rides as `X-EM-Token` on all three dials.

Controller enforcement (`em_linkauth.decide`, called by `_link_auth_ok`): presented-but-wrong token always rejects; stored-token-but-none-presented is allowed (the credential push itself rides the plain shell plane, and rejecting there would deadlock the rollout); a token presented for a device with NOTHING on record is **ignored, not rejected**. Rejecting it made deleting a device a one-way door, since delete takes the token with the row while the device keeps re-reading its credential file, and the refusal covered the shell plane the controller would have fixed it over. It also bought nothing: a connection presenting no token at all is already allowed, so an attacker just omits the header. `REQUIRE_DEVICE_TLS=1` flips the posture to TLS+token mandatory and is unaffected by that: a deleted device is still refused there and needs credentials pushed over USB. Flip it only when every device shows `wss (TLS)` in the dashboard (Status tab "Link" row; `linkTls` in `/api/devices`).

Credential delivery: the provisioning wizard installs credentials over adb pre-first-contact (`POST /api/provision/tls_credentials` mints the token + pending device row from the serial); already-fleet devices get the dashboard **Secure link** action (`POST /api/devices/{id}/secure_link` — shell-plane file push, then a connection bounce to redial over wss).

### Device audio pipeline

Each mic buffer passes through, in order:

```
raw 9ch S24_3LE → beamformer + fixed mic gain (micGainDb, applied to 24-bit samples) → mono S16_LE → [AEC] → [AGC] → [VAD gate] → /data WebSocket
```

Note the real buffer cadence: GoTinyAlsa's `GetAudioStream` reads the whole ALSA buffer per chunk (PeriodSize 512 × PeriodCount 5), so the mic pipeline runs on **160ms batches of 2560 samples**, not single 32ms periods. Anything assuming 512-sample buffers must handle multiples (this silently disabled AEC for four releases — see `aec.Process`).

The always-on wake stream (`mic_start` without `lock_mic`) is **ungated and AGC-free**: every 32ms period is sent continuously (batched into 80ms frames) so openwakeword scores an uninterrupted stream, and no adaptive gain state can drift with room noise. The VAD gate and AGC apply only to bounded `lock_mic` turn streams (button-triggered), which get a fresh `ResetAGC()` per stream.

- **Beamformer** (`internal/beamformer/`) — selects the perimeter mic with the highest onset energy ratio (fast/slow EWMA) at voice turn start, then locks for the duration. Its `extractChannel` also applies the fixed mic gain (`micGainDb`, default +24dB) against the full 24-bit sample before quantising to S16 — captured speech sits at ~−70dBFS, so gain must happen pre-truncation to recover real resolution. `vadThreshold` stays in pre-gain units (the device scales it by the gain internally). **It is a selector, not a summing beamformer, and that is settled — do not propose delay-and-sum.** A frequency-domain implementation (exact FFT phase shifts, no interpolation artefacts) exists in `device/tools/bf_capture` and was measured as only marginally better than mic selection. The reason is the 72mm aperture, not the code: diffuse-field noise coherence is 0.84–0.99 below 1.5kHz where speech energy lives, so a sum has almost nothing uncorrelated to cancel, and 36mm adjacent spacing puts spatial aliasing at 4.76kHz — a working window of roughly 2–4.7kHz. Superdirective/differential beamforming is the only class that works at this aperture and it trades against white-noise gain (20dB+ amplification of sensor self-noise) on unmatched capsules across four ADCs. Full derivation and the coherence table are in SETUP.md's mic-array section (SETUP.md is the architecture reference; the chronological log is JOURNAL.md, the rooting prerequisites docs/rooting.md). **Far-field reach is therefore not a beamforming problem here** — it is room noise floor, distance and placement; the single-channel levers (`nsAsr`, wake model) are the ones that exist
- **AEC** (`internal/aec/`) — speexdsp echo canceller (vendored C, SpeexDSP-1.2.1), whole mic path including the wake stream; far-end reference tapped at the speaker ALSA write (every period incl. silence), delayed by `aecDelayMs` — **keep 0**: the mic side's 160ms batch reads absorb the speaker's output latency, and higher values make the echo non-causal (zero cancellation). The mic ALSA ring is only 160ms deep, so >160ms capture stalls silently lose whole batches (~every 20–30s in steady state, load-correlated); an occupancy governor trims the resulting reference backlog **without resetting the filter** — the trim restores the alignment the filter converged against, and the reset that used to live there thrashed convergence to ≤5dB (the v2.7.8 fix). `[aec] att=`/`far:` telemetry logs ~1/s during playback; `[mic] clock/stall` lines track capture loss. `far:` carries `rms`, `mean` and `peak` — **rms alone cannot tell audio from a constant offset**, since both read high, and that ambiguity cost an evening on #117 where the device was writing rms≈4000 to a codec while every speaker stayed silent. `mean≈±rms` with a small peak-to-peak is a DC offset; `mean≈0` with peak well above rms is real audio and the fault is downstream. Note this tap sits after the (L+R)/2 downmix and 3:1 decimation, so DC survives intact but `peak` is mildly smoothed — read it as a floor. It reports only while `aecEnabled`, so a diagnosis that needs it must not have AEC turned off. Default off (`aecEnabled`); ~14dB per response, held across turns
- **Barge-in** (controller-side `_barge_watcher`) — wake word spoken during TTS cancels playback (device does a stateful `speaker_flush`: drains buffer + discards until stream EOS, since the rest of the stream is typically still in TCP buffers; controller-side, both `stream_speaker` and the post-playback drain sleep race `cancel_event`). `bargeInThreshold` is used as-is and sits *below* `owwThreshold` by design (0.05–0.10): echo at the mic is ~25dB louder than the person, so speech-over-TTS scores are depressed (~0.3–0.5 observed), while converged self-echo scores 0.002–0.003. **A barge must abort HA's run before starting the interrupting turn** — see below
- **AGC** (`internal/processor/`) — lock_mic turns only; release is frozen during silence (RMS speech flag), preventing noise floor amplification. (Device-side RNNoise NS was removed 2026-07-12 — noise suppression is controller-side now: `em_ns.py`/DTLN on the ASR-bound stream, per-device `nsAsr` flag)
- **VAD** (lock_mic turns only) runs on pre-NS/AGC audio; opens gate after `VAD_SPEECH_MS` of speech, closes after `VAD_SILENCE_MS` of silence, then sends an end-of-speech sentinel

### Controller audio pipeline

1. **Wake word** — openwakeword (ONNX) runs in a thread executor per device on `mic_queue`. When 2+ devices are connected, `em_arbiter.py` applies **first-detector-wins** suppression: the first device to cross threshold answers *immediately* (no added latency, the claim is synchronous) and any other device detecting within `wakeArbitrationMs` (default 700, 0 = off) stands down and logs "Wake ceded". The claim is released at turn end. Do NOT reinstate the original best-SNR-after-a-wait design: it taxed every wake ~364ms (it gated on devices *connected*, not in earshot) and field data showed SNR at detection was indistinguishable across devices (0.9/1.15/0.93) while the SNR winner produced a worse transcript than the first detector.
2. **Voice turn** — on wake or dot-button: drain stale frames → acquire `voice_lock` → stream mic to HA via the ESPHome satellite → receive TTS URL → **incrementally** fetch + ffmpeg-decode straight to 48kHz mono → EQ (`em_eq.py`) → stream back as 0x02 frames. `_stream_tts_audio` pipes the HTTP response into one long-lived ffmpeg and yields PCM as it decodes, so playback starts while HA is still generating — neither the encoded response nor the decoded speech is accumulated. **A retry is only safe before the first PCM has been emitted**; `_fetch_tts_audio` remains for callers that genuinely need the whole buffer
### Ducking: music and voice are separate planes on the device

**A voice turn DUCKS music; it does not pause it** — on firmware announcing
the `audio_mix` capability. Music rides its own frame types (`0x04`/`0x05`)
into a second buffer on the device, and the two are mixed at the ALSA write.

It has to be device-side, and that is the whole design constraint. `LEAD_S` =
4.0s means the next four seconds of music are already in the device's buffer
when a wake word fires, so **audio that has left the controller cannot be
ducked by the controller**. Every controller-side alternative buys ducking by
shortening that lead — giving back the stall protection it exists for, during
the seconds the user is listening most closely.

What this removes, not just improves: pausing needed a seek to resume, and a
Music Assistant flow stream **cannot seek**, so a 28s turn cost 28s of the
song and a long one landed in the next track. Behaviour differed by source
with no way for the user to tell which they had.

- **Voice is never attenuated**; only the bed under it. The sum saturates
  rather than wrapping (a wrap turns a loud peak into full-scale opposite
  polarity, far worse than the clipping).
- **The gain ramp is a constant slew, not proportional.** `(target-gain)/n`
  per period is an exponential approach, which made "4 periods" a time
  constant rather than a duration — measured at 31 periods (1.3s) to settle
  against the 170ms intended. Gain is interpolated per SAMPLE across the
  period: a step at a period boundary is a click, landing on exactly the
  moment the user started speaking.
- **`duckDb` is config** (default −18dB, Playback section), because it is a
  taste parameter that wants tuning by ear in a real room — same reasoning as
  the LED meter response curve.
- **`music_flush` vs `speaker_flush`.** A genuine stop/pause flushes the
  MUSIC plane; `speaker_flush` would cut the response and leave the music
  playing. A voice turn sends neither — flushing discards the buffered audio
  that makes ducking instant, and on a non-seekable stream it cannot be
  recovered.
- **`MediaSession.ducked` changes what turn end must do.** On the pausing
  path `interrupt()` had already paused, so a deferred user "pause" needed no
  wire action. When we duck, nothing was ever paused — the pause has to
  actually happen at release, or it is silently dropped and the music plays
  on.
- **Music does NOT count as "streaming"** for the device's wake threshold
  (`IsStreaming` is voice-only). It is a quiet continuous bed, not a response
  being talked over; reporting it would drop the device's wake bar for the
  length of a song.
- Taps see the MIXED output, which is more correct than before: the AEC
  far-end reference is what needs cancelling from the mic, and with music
  under a response the echo is the sum.
- `reported_state` stays for firmware that cannot mix. On the ducking path it
  is simply never triggered, because nothing is ever paused behind HA's back.

3. **Media playback** (`em_player.py`) — the HA `media_player` entity accepts `play_media`/browse (PLAY_MEDIA+BROWSE_MEDIA feature flags): ffmpeg subprocess streams s16le/48k/mono, fed to the same 0x02 plane, paced to `LEAD_S`=**4.0s** ahead of realtime. That is sized against the device's own depth (`audioChanDepth` 128 periods × 42.7ms ≈ 5.46s) leaving ~1.4s headroom so the feed can never outrun `audioCh`. It was 1.5s until 2026-07-25, which left ~4s of hardware buffer unused and let measured 1.8-2.6s link stalls drain it into audible gaps. **The lead is NOT what makes pause/stop/voice-preempt instant** — `speaker_flush` drains the device buffer and the discard-until-EOS contract swallows what is still in TCP; the old comment misattributed that. Resume passes `-ss` before `-i`, an INPUT seek: ffmpeg does NOT ignore a seek it cannot perform, it decodes and discards until it reaches the timestamp, so a 173s bookmark on a non-seekable live stream (Music Assistant flow) is 173s of silence — a first-chunk deadline (`SEEK_STALL_S`) catches that and rejoins the live edge. Pause = speaker_flush + position bookmark, resume = ffmpeg `-ss` (live streams rejoin the live edge); teardown always EOSes the stream (flush-discard contract, same as barge-in). **Voice turns and announcements OWN the speaker**: `interrupt()` takes ownership and `resume_interrupted()` releases it. Ownership is taken unconditionally, even with nothing playing — the old behaviour only paused what was ALREADY playing, which did nothing to stop something *starting* mid-turn, and "play some jazz" runs the intent **before** HA generates the spoken reply, so `play_media` lands while the TTS is still coming and puts music on the same 0x02 plane as the response. While owned, `play`/`resume`/`pause`/`stop` record the user's intent instead of touching the wire; the release applies it, **last write wins** ("play jazz… actually, pause"). A user command **overrides** the auto-resume — theirs is an instruction, `resume_after` is bookkeeping — which is what makes pausing during a barge-in possible at all (issue #53: `MediaSession.pause()` returns early unless PLAYING, so the command was discarded and then contradicted by the resume). Deferred commands still push the **intended** state to HA (`push_intent`) so the entity is not stale for the length of a turn. The feed must NOT set `device.speaking` (that makes the wake loop drop frames — deaf for a whole song); wake-over-music scores against `bargeInThreshold` when barge-in is enabled, same physics as barge during TTS. Music EQ runs through `em_eq.StreamingEQ` (chunk-carried filter state — per-chunk `apply()` would click at boundaries).

    **Never send HA a hardcoded `MediaPlayerState`.** The feed announces `playing` exactly ONCE, when the decoder starts producing audio, so anything sent afterwards becomes HA's last word. Two places asserted a constant `IDLE` — turn end, and every device volume report — and each left the entity showing idle over audible music (issue #53, twice: fixing the first instance is how the second survived). Use `_media_state_msg()`, which reads em_player truth. `tests/test_deploy.py` forbids the shape, allowing only the documented optimistic `PLAYING` for `play_media` and `ANNOUNCING`.

    **What HA is told is not always our internal state — use `em_player.reported_state`, never `state`.** While a turn owns the speaker our state is `PAUSED`, but that pause is *ours* and invisible to the user, so the entity must keep reporting `PLAYING`. Reporting the truth made HA answer "it's already paused" to a spoken pause and **never send the command** — so `em_player.pause()` was never called, no intent was recorded, and `resume_after` put the music back (issue #62). Note #53's fix was correct and still did not cover this: it makes a user pause win over the auto-resume, but the pause never arrives. The entity was never wrong about the state machine; it was wrong about what the user could see, **and HA acts on the latter**. Once the user does issue a command `pending` holds it and the real state is reported again. `_media_state_msg()` must use the same source — it rides volume reports and turn end, and would otherwise undo the fix from the other direction.

    **`SOURCE_STALL_MS`** (500ms) times the READ from ffmpeg, and exists to answer a question the device cannot: a device-side gap between frames arriving looks identical whether the controller had nothing to send (source starving — a Music Assistant flow) or the link swallowed it, and `send_ms` cannot settle it either since a socket write completes near-instantly however slow the wire is. A device gap WITH a source stall logged at the same moment is upstream; without one it is the link. Only the read is timed — the pacing sleep must stay outside it, or every healthy stream reports as permanently stalled.

    **`DATA_RECONNECT_GRACE_S`** (3s) rides out a brief data-plane drop instead of discarding the rest of the audio (#28). The budget is per STREAM, armed by `begin_data_stream()` and spent down by `send_data` — **never per frame**: `send_data` runs once per audio period, so a per-frame wait makes a genuinely-gone device stall every remaining frame in turn, draining a stream for hours while holding the voice lock.
4. **Speaker** — the wire carries **mono** 48kHz; `_fetch_tts_audio` decodes at the wire rate (the satellite declares `supported_formats` 48k/mono/FLAC so HA transcodes at source when it can; ffmpeg resamples otherwise — no numpy resample step anymore). The device duplicates L=R at the ALSA write (stereo ALSA config is an I2S/codec constraint, not a wire one). Device buffers ~5.5s (`audioChanDepth`) and holds playback until ~1s is queued or EOS arrives (`primePeriods`) — WiFi-stall protection for marginal links

### Key Go packages

| Package | Role |
|---------|------|
| `cmd/server.go` | Entry point: wires hardware, callbacks, and clients together |
| `internal/client/control.go` | WebSocket client to controller `/control` — registration, message dispatch |
| `internal/client/data.go` | WebSocket client to controller `/data` — mic streaming, speaker playback |
| `internal/server/` | Local state machine: mute, volume, LED mode priority |
| `internal/config/config.go` | Global runtime config; env var defaults, overridden by controller push |
| `internal/bindings/` | Hardware drivers: mic PCM, speaker PCM, LED I2C, button evdev |
| `internal/wakeword/` | openWakeWord streaming feature pipeline (mel ring → 76-frame windows → embedding ring → classifier). Pure Go: inference sits behind the `Inferer` interface so the buffering is host-testable with no ONNX/cgo. Validated tensor-for-tensor against Python via a golden fixture (`testdata/`, regenerate with `gen_fixture.py`) |
| `internal/wakeword/ort/` | The `Inferer` implementation: ONNX Runtime via cgo. The library is **dlopen'd at runtime, never linked** (only the MIT C header is vendored) so a device without it boots normally and falls back to controller-side wake word — verified by the ARM binary needing only libdl/liblog/libc with zero undefined `Ort*` symbols. `DefaultOptions` (1 thread, XNNPACK, `allow_spinning=0`) is the measured optimum: 37.7% of one core against 243% for ORT's defaults. Don't "fix" the thread count — more threads lowers latency and *raises* CPU, the wrong trade for duty-cycled work |
| `internal/wakeword/shadow/` | On-device scoring that reports but never acts (see "On-device wake word"). `Push` must never block: inference runs on its own goroutine and drops frames when behind |
| `internal/wakeword/fixture/` | Shared golden-fixture parser, tolerance policy and `Verify`. Used by both the host test and `tools/oww_probe`, deliberately — the probe's answer is the trusted one because it runs on hardware, so it must be exactly as strict as the test by construction. Tolerances are relative to the **tensor's** scale, not per element: per-element relative error is meaningless for tensors straddling zero |
| `internal/bindings/als/` | Ambient light (ams **TSL2540** on i2c). Android does not expose it AT ALL — `dumpsys sensorservice` reports an empty list, nothing under `/sys/class/sensors`, no input device; it is visible only on the raw i2c bus, the same shape as the mute LED being on a different GPIO than the vendor HAL believed. Resolved **by name, not address** (`0-0039` is an enumeration accident). **The bus listing is not a hardware inventory**: both ALS names are registered by Amazon's board file, so a `tsl2540` at 0x39 and a `tsl2584tsv` at 0x29 appear on every unit whatever is soldered on (`modalias` is static kernel data). Which one answers differs by batch — ours have the 2540 and nothing at 0x29 (`taos_probe() err = -6`, ENXIO), the `G090LF096` batch has the 2584 instead, reachable only through IIO at `/sys/bus/iio/devices/iio:device0` (#90). A second-sourced part, not a driver fault, so the answer is to read the IIO sensor too, never to loosen the match to a `tsl` prefix. The **boot log is the real inventory** — both drivers probe on every unit and log what replied — but `dmesg` rolls, so it needs reading soon after a reboot. Never `unbind` the driver to experiment: it succeeds, leaves the `als_*` attributes in place, and the next read hangs the device until a power cycle. `Lux()` returns **nil, never 0** — a covered sensor reads a genuine 0. `Watch` reports a step change immediately (25% relative, 10-lux floor, measured noise ±1.5%); the steady value rides the ~30s stats tick. `Report()` says **why** there is no sensor (`ok`/`no_chip`/`no_attribute`/`unknown`, plus every i2c name it saw) and rides the register message as `ambient_light_status` — absence used to be logged only to the device's own stdout, which support bundles do not collect, so two users could not be told apart without a shell session (#90). The whole bus is enumerated **before** matching: returning at the match truncated the list on working devices, which is exactly the side you compare against |
| `internal/bindings/jack/` | Headphone jack detect (`/sys/class/switch/h2w`, mediatek accdet). Polled, not evented — the ACCDET input node reports no keys on this hardware. Exists for ONE job: accdet mutes `Ext_Speaker_Amp_Switch` on insert (correctly) and **nothing turns it back on**, so the speaker stayed dead until the next boot (#80). Output routing itself is done by the jack's own switch contacts — a response was heard in headphones while the mixer still read `Headphone_Speaker_Mux=Speaker`, so those controls do NOT describe where audio goes and nothing should drive them |
| `internal/wifi/` | Safe WiFi network change with auto-rollback (wifi_change/wifi_commit/wifi_scan control messages; pending-marker recovery at startup). Reload path is `svc wifi disable/enable` ONLY — see package comment for the hardware-proven constraints |
| `pkg/led/`, `pkg/mic/`, `pkg/speaker/`, `pkg/buttons/` | Hardware abstractions (interfaces) |

### Key Python modules

| File | Role |
|------|------|
| `em_controller.py` | WebSocket server, `Device` registry, voice pipeline, mDNS |
| `em_api.py` | aiohttp HTTP API + dashboard SPA, OTA, shell proxy |
| `em_db.py` | SQLite persistence (devices, config, logs, users) |
| `em_auth.py` | Session auth with bcrypt |
| `em_eq.py` | Parametric EQ applied to TTS audio before playback |
| `em_oww_assets.py` | On-device wake word asset distribution — plans what a device needs (runtime + shared models + classifiers), what to push and what to evict. Pure logic; the two transports live in `em_api.py` |
| `em_shadow.py` | On-device wake word shadow mode — correlates device-reported threshold crossings with the controller's own detections (clock domains, match window, consume-on-match) |
| `em_scenes.py` | LED ring scenes — resolves `ledScene`/`ledListenColor`/`ledThinkColor` config into render-ready listening/spinner frames |
| `em_esphome.py` | ESPHome-mode satellite servers (`EchoMuseSatellite`, `DeviceESPhomeServer`) |
| `em_arbiter.py` | Multi-device wake arbitration — pools same-utterance detections, best SNR answers |
| `em_player.py` | Media playback sessions — `media_player.play_media` → streaming ffmpeg decode → paced 0x02 feed; pause/resume/stop; voice preempts music (`interrupt`/`resume_interrupted`) |
| `em_config_sections.py` | Fleet-vs-device config scoping — the six sections, `STATE_KEYS`, and the merge that resolves a device's effective config |
| `em_tap_burst.py` | Coalesces a burst of action-button taps into one single/double/triple event. The window is restarted per tap and `enabled()` is re-checked at expiry, both correct. **The window is timed at the CONTROLLER, on arrival**, so the gap it measures is the real gap plus the RTT difference between the two taps — 26.4% of probes on this fleet exceed 200ms, which is why double/triple are unreliable below ~350ms (#115). The fix is a device-measured gap, the same reasoning as `heldMs` |
| `em_recordings.py` | Utterance capture storage — WAVs in `recordings/` beside the DB, per-device file-count retention, ownership-checked path resolution |
| `em_turnclock.py` | When a voice turn stops waiting, as a pure function. **The no-speech window is measured from the FIRST REAL AUDIO FRAME, not from turn start** — those answer different questions, and measured from turn start a slow link masquerades as a silent user. A 1373ms delivery gap (#139) shortened a 5s window to 3.6s and answered `no_speech` to someone mid-sentence, with the audio captured perfectly on the device and TCP holding it. `FIRST_AUDIO_GRACE` bounds the other side so audio that never arrives still ends the turn |
| `em_runbarrier.py` | Serialising ESPHome pipeline runs across a barge-in, as a pure state machine. The protocol carries **no run identifier**, so the satellite is what keeps two runs from overlapping — see "Barge-in serialisation" below. Split out for `em_linkauth`'s reason: the suite cannot import `em_esphome` |
| `em_announce.py` | Running an HA announcement to completion. Owns the two rules that pull against each other — never reply early, always reply — because `VoiceAssistantAnnounceFinished` is HA's completion signal and HA **blocks** on it |
| `em_linkauth.py` | The device-link auth decision as a pure function. Split out of `em_controller._link_auth_ok` so it is testable: the suite does not import em_controller, so this was security logic with no coverage until it orphaned a device |
| `em_ble_proxy.py` | BLE proxy ESPHome servers — a second, separate ESPHome device per Echo (own port from the shared counter, own mDNS, MAC = serial-derived with the locally-administered bit flipped). Forwards `ble_adverts` control messages from the device's passive scanner (`device/internal/bluetooth`, raw HCI over `/dev/stpbt`; enabling durably disables Android's BT stack) to HA as raw advertisements. Lifecycle = idempotent `reconcile()` driven by `bleProxyEnabled` |
| `esphome/` | ESPHome native API protocol layer (framing, handshake, vendored protobufs) |

## On-device wake word (shadow mode)

The Echo can run the wake model itself. `owwOnDevice` = `off` (default),
`shadow` or `on`; an unknown value normalises to `off` at BOTH ends rather than
being guessed at — the two plausible guesses are "score silently" and "start
triggering", and one of those is a live behaviour change on a device that
cannot honour it. Neither end may assume the other is the careful one.

**`on` is gated on the `oww_trigger` capability, which is separate from
`oww_shadow` on purpose.** Shadow shipped first, so there is firmware in the
field that scores and reports without being able to act on it; offering those
`on` produces a device that scores perfectly and never answers.
`em_shadow.effective_mode` degrades `on` to `shadow` when the capability is
absent — never to `on`, which would leave the controller waiting for wakes the
firmware has no code to send while no longer acting on its own. That is a wrong
answer rather than the old behaviour, which is the line the whole capability
rule is drawn along.

### `on` — the device decides, the controller keeps watching

The device sends `oww_wake` (score, the threshold it actually cleared, and how
long AGO — never a timestamp) instead of `oww_shadow_cross`. It lands in
`Device.pending_wake` and the wake listener acts on it on its next mic frame
(~80ms), because that is where turn setup lives: capture routing, beam lock and
arbitration have to happen together, and driving them from the control-plane
handler would be a second copy of the most delicate sequence in the controller.
`em_shadow.decide_wake_source` is the decision, pure and tested, for the reason
`em_button.decide` and `em_linkauth.decide` are.

- **The controller keeps scoring, and its detections stop triggering.** Its
  score still records whether it agreed (`turns.ctrl_wake_score` /
  `ctrl_wake_delta_ms`, schema v17) — the comparison that justified shipping
  this, with the roles inverted, and the only place a *controller* miss can be
  seen at all. Without it, turning a device `on` would silently end the
  measurement: every turn would show a device score with nothing to compare
  against, which reads as perfect agreement rather than as no data.
- **It is also what leaves barge-in alone.** Barge is scored controller-side
  over the turn's own audio (`_barge_watcher`) and is untouched by this.
- **`last_wake_mono` is the CROSSING instant, not arrival.** Using arrival
  would fold the network hop into every comparison and every arbitration
  decision, which this fleet's measured 1.1–2.6s RTT excursions make certain
  to matter.
- **Mute is checked on the device** (`onWakeCrossing`), not only controller-
  side. The existing `mic_start` refusal plus the hardware ADC mute already
  make a muted wake harmless, but "harmless" still means the ring lights up and
  HA runs a pipeline because a muted device thought it heard something. The
  crossing is still *reported* — it is real data about the detector.
- **A pending wake expires** (`MAX_PENDING_WAKE_S`, 4s, measured from the
  crossing). A wake that stale means the person has finished speaking, so
  acting on it answers into silence; expiry logs the age, which is the
  instrument for whether the trigger needs more slack.
- The trigger label is `wakeword-dev(score)`, which still matches every
  existing reader's `wakeword` prefix — including `_persist_turn`'s shadow
  block.

**Arbitration is NOT yet corrected for this.** `_wake_arbiter.claim` still
compares arrival order, so on a multi-device fleet a device can lose a 700ms
window because its claim was late and the wrong room answers. The fix is to
compare RTT-corrected times, never revoke a granted claim (that cuts a turn
already speaking), and hold the window longer than it is measured. Single-device
use is unaffected — solo fleets skip the window entirely.

**Shadow mode scores and reports; it never acts.** It exists to answer whether
on-device detection is good enough to trust, by comparing both detectors on the
same audio. The tap sits where the ungated wake stream's frames are written to
the wire, so the device scores byte-identical 80ms frames on identical
boundaries — a score difference can then only be the engine, not the framing.

Three things are load-bearing:

- **Inference must never run on the mic goroutine.** It costs ~31ms per 80ms
  frame, the mic loop reads 160ms ALSA batches, and the ring is only 160ms
  deep — two frames inline would spend 62ms of that budget and risk the capture
  stalls that lose whole batches. `shadow.Scorer.Push` hands off to a buffered
  channel and returns; the scorer goroutine **drops frames and counts them**
  when it falls behind. A shadow run that drops frames is informative; one that
  stutters the microphone is not.
- **Nothing is sent per frame.** Threshold crossings go immediately (they are
  rare — a refractory period collapses each utterance to one — and their whole
  value is the timing). Everything else is a window summary riding the existing
  ~30s stats tick, so the DB cost is one extra upsert per 30s per device.

  The window summary carries `maxInferMs` and `maxGapMs` (schema v16) because
  `dev_drops` alone had stopped being able to answer its own question — turn
  bursts, core hotplug and controller redeploys were each falsified by
  measurement. A drop means the 8-frame (640ms) queue overflowed, which has two
  causes needing opposite fixes: the slowest single inference is the CONSUMER
  stalling, the longest gap between frames arriving is the PRODUCER bursting.
  **Maxima, not averages** — a stall IS the tail, and a 700ms event averaged
  over 375 normal frames disappears. They are `atomic.Int64`, not mutex state,
  because `Push` runs on the mic goroutine; and the gap is measured in
  `enqueue` so one site covers `Push` and `PushBytes` alike, the same reason
  drops are counted in exactly one place. Note the first frame must record NO
  gap: a zero-valued `lastPush` would report a gap of however long the process
  had been running and point at a producer stall that never happened.
- **The device never sends a timestamp.** An Echo's wall clock is bogus before
  NTP, so it reports how long *ago* a crossing happened and the controller
  converts against its own monotonic clock — same reasoning as the RTT
  instrumentation.

**Thresholds must match or the comparison is meaningless.** The controller drops
its wake bar to `bargeInThreshold` while the speaker is streaming (echo at the
mic is ~25dB louder than the person, so speech-over-TTS scores are depressed), so
the device mirrors that: `shadow.Scorer.SetBargeThreshold` uses the lower bar
while `PcmSpeaker.IsStreaming()` is true, and never *raises* the bar if
misconfigured above the normal one. The device reports the threshold in force
with each window summary; it lands on the turn as `dev_threshold` (schema v15).
`turns.wake_threshold` now records the **effective** threshold the wake actually
cleared, not the nominal one — recording 0.5 for a wake that fired at 0.055 made
rows self-contradictory (present in data since at least 2026-07-25) and made
every barge-in look like an on-device miss. The activity rollup therefore reports
three buckets, not two: agreed, missed, and **not_comparable** (controller used a
lower bar, or the device's threshold is unknown).

Correlation (`em_shadow.ShadowTracker`, schema v13) happens at turn-persist
time, not at detection: the crossing report can land after the wake it belongs
to, and by turn end it has had seconds to arrive. The nearest crossing within
`MATCH_WINDOW_S` (2.0s) wins and is **consumed**, so two turns in quick
succession cannot both be credited to one crossing. The window is loose on
purpose — both detectors see the same frames but not in the same detector
*state*, since the controller drops wake frames while a turn or TTS is in
flight, and a false "miss" argues against a feature that is actually working.
`turns.dev_shadow` records whether the device was scoring at all, which is what
separates "the device missed this" from "the device was not looking";
`wake_counters.dev_*` carries the hourly view, where crossings with no matching
turn are the false-accept side that per-turn rows structurally cannot show.

Requirements and cost: ONNX Runtime plus the three models must be installed at
`shadow.DefaultDir` (`/data/local/share/echomuse/oww`, override `EM_OWW_DIR`)
— they are **not** in the firmware, since 12.3MB would double the OTA payload
and both A/B slots. Absence is an ordinary condition, logged once, and the
device carries on with controller-side wake word. `device/tools/oww_probe`
verifies a device reproduces Python and reports the real CPU cost. It costs
~38% of one core permanently on top of the ~18-20% mic-pipeline baseline, so
**enable it on one device at a time**.

**The scorer pointer must be re-read PER FRAME, never cached for a stream.**
A config push replaces the scorer and **closes** the old one, so a mic stream
holding the pointer it captured at `StartMic` is feeding a dead object. This
cost two bugs in succession on 2026-08-16, and the second is the instructive
one:

- `Close()` used to close the channel `enqueue` sends on, so the next 80ms
  frame panicked the process. It restarted in ~6s, opened a fresh mic stream,
  picked up the new scorer, and worked — the crash was **accidentally
  self-healing**.
- Making `enqueue` drop silently removed the crash *and* the recovery. The new
  scorer then received nothing and detection stayed dead until the next
  `StartMic`, which only follows a voice turn, which cannot happen because the
  wake word is dead.

So `Close()` signals a dedicated `quit` channel and never closes `ch`, AND the
push site re-reads `d.ShadowScorer()` per  frame. A mutex read per 80ms is
nothing beside the inference it feeds. Both halves are needed; either alone
leaves a device that goes deaf or panics. The comment that justified caching
("a stream that began before the change keeps using the scorer it started
with") described exactly what made it fatal.

**A missing classifier used to silently deafen a device under
`owwOnDevice=on`.** The device cannot score without the model, and the
controller has stood down and no longer triggers on its behalf — so nothing
fired, nothing warned, and the dashboard reported the device as healthy. That
is a degradation to *no* behaviour, which the capability rule above exists to
forbid. Selecting a wake word a device was never provisioned with is enough to
produce it, and that is an ordinary dashboard action.

**Bouncing the HA connection flaps EVERY entity for that device.**
`update_oww_model` drops and remakes the connection so HA re-reads the wake
word, and that is the only lever the protocol offers — HA calls
`_update_satellite_config()` from `async_added_to_hass` and nowhere else. The
cost is that the voice assistant, media player, event and sensor entities all
go unavailable and back in the same instant.

For the **event** entity that is user-visible and looks like a fault: HA's
`EsphomeEvent._on_device_update` deliberately writes state on reconnect
("Event entities should go available directly when the device comes online"),
restoring the last event's timestamp. `_trigger_event` is NOT called, so HA
does not think a new event happened — but a **state-triggered** automation sees
`unavailable` → timestamp and fires. Reported 2026-08-17 as "changing the wake
word triggers a long button press"; the controller had sent no event at all.
The user-side fix is `not_from: [unavailable, unknown]`, documented in
docs/configuration.md. Worth remembering before adding any new bounce.

**The fix is install-before-switch, not a fallback.** A device is never told
about a new `owwModel` until the classifier is on it
(`em_api._hold_back_oww_model` swaps the key back to the current model, and
`_install_then_switch` pushes the real config once the file has landed). The
device keeps listening for its CURRENT wake word, on-device, throughout; if the
install fails it simply stays there and says so loudly.

The first attempt stood the device down to controller-side scoring while the
model installed. That works and was rejected: it silently overrides a setting
the user chose, and the dashboard goes on reporting `owwOnDevice: on` — the
same "reports healthy while something else is true" shape the capability rule
exists to forbid. Note there is no privacy difference between the two modes
(the device streams the wake audio either way, and the controller scores it in
`on` mode too — that is what `turns.ctrl_wake_score` records), but a silent
override is a trust problem regardless.

Only devices that actually score locally are held back — with
`owwOnDevice=off` the file is irrelevant, so the change stays instant. Both the
old and the incoming mode are consulted, or a save that enables on-device
scoring while changing the wake word slips through on the old mode.

`em_shadow.effective_mode` also takes `model_ready` alongside
`trigger_capable`, as a **backstop** with no current writer: install-before-
switch removed the case that set it, and its intended writer is the
reconcile-on-connect pass designed in #191 — the first thing that will actually
know what a device has. A known-missing model degrades to **`off`, not `shadow`** —
shadow cannot score either, so degrading to it would be the wrong answer
dressed as a fallback; only `off` puts the controller back in charge of
triggering, which is the one arrangement that still answers the user. It
defaults **True**: absence of evidence is not evidence of absence, and standing
every device down because the controller has not looked would be worse than the
bug.

The hold-back is invisible at the call site — the config push looks entirely
ordinary and the whole guard is that one key was swapped out first — so tests
pin the ordering, the capability gate, and that a failed install returns rather
than falling through into the switch. The install runs as a background task:
blocking the config save on a multi-megabyte shell-plane push would time out
the request without making anything safer, and nothing is degraded while it
runs.

The rest of #191 — installing all four stock classifiers, custom slots,
reconcile-on-connect and a per-device Repair action — is designed on the issue
and not yet built.

### Asset distribution (`em_oww_assets.py`)

Installing those files is automatic. `em_oww_assets` plans (pure, unit-tested);
`em_api` carries it out. Two transports, one plan: the **provisioning wizard**
pushes over USB/ADB (a fresh device is not connected to the controller yet, and
USB suits 15MB far better than a base64 heredoc), and **fielded devices** use
the shell plane from the device's Updates tab. The wizard step is **mandatory**
— a device advertising `oww_shadow` without the assets is exactly the "I
enabled it and nothing happened" this removes.

- The ARM runtime is **vendored into the controller image**, pinned by AAR
  sha256 (`onnxruntime-android` 1.19.2), so devices never need internet. The
  models come from the installed openwakeword package or `oww_models/` — no
  second copy to keep in step.
- **md5 is the only definition of success**, both transports. Push to `.part`,
  rename only on match: a truncated file is the right size and fails later at
  `dlopen` with an error naming nothing.
- **Four classifier slots, LRU by device mtime** — no controller-side
  bookkeeping to lose across a restart. The selected model is **pinned**;
  evicting it is the one outcome that breaks a device rather than costing a
  re-push. Only files positively recognised as evictable classifiers are ever
  deleted.
- Free space is checked against **what actually needs sending**, so a device
  that already has everything is never blocked. Read it with
  `parse_free_mb`, never an awk field index — busybox wraps a long filesystem
  name onto its own line, so `$4` is the *percentage* on these devices, which
  parsed as "unknown" and silently disabled the check.
- **`TransferResult` is truthy-compatible so existing `if not …` call sites
  keep working — which is exactly how a call site that treated it as a LIST
  reached a release.** `_sync_oww_assets` assigned the per-file transfer
  result over its own `pushed` accumulator, so the first file replaced the
  list and the append raised `AttributeError`. Every classifier push 500'd,
  and provisioning is the only other path that installs one, so a device
  could never be given a wake word it had not been provisioned with. Pinned
  by test.
- `DEVICE_DIR`, the shared model names and the classifier stem rule are pinned
  against the firmware constants **by test**. Drift installs assets the device
  never looks for, and the only symptom is shadow mode silently never starting.

## The external audio jack

Three separate faults, all fixed 2026-08-09 (#80), and one non-fault worth
knowing so nobody builds it.

**Boot with a plug inserted used to strand the whole device.** Android's
mediaserver claims the speaker PCM when a headset is present, and ALSA parks a
blocking open behind it with no timeout:

```
/proc/<pid>/task/<tid>/wchan          -> snd_pcm_open   (parked indefinitely)
/proc/asound/card0/pcm23p/sub0/status -> PREPARED, owner_pid: 659
fuser /dev/snd/pcmC0D23p              -> 258 (/system/bin/mediaserver)
```

`main()` initialises the speaker **before** `SubscribeToButton`, mDNS and the
control client, so one held device cost everything — no buttons, no wake word,
no registration. That is the whole of the "no wake word and no working
buttons" report. `Init()` now runs `stop media` first (the same stock-service
takeover as `stop mixer` beside it and `stop smarthomewifid` in `main`) and
waits on the substream status before opening.

**`stop media` does NOT stick, and the fix does not depend on it doing so.**
Android restarts mediaserver — measured on hardware: `init.svc.media` reads
`running` again, with a live pid, while our server still owns `pcm23p` in
`RUNNING` state. What makes this work is winning the race ONCE and then
holding the device for the life of the process, and `Init()` re-runs
`stop media` on every start, so an OTA or a supervisor restart gets the same
treatment. Do not "improve" this into a permanent disable: mediaserver
returning is what keeps Amazon's audio HAL — and therefore the DSP and the
I2S clock — initialised, which SETUP.md's Audio Notes describe as load-bearing.

**Android still reacts to jack events.** With mediaserver back, an insert
makes its `AudioOut_2` thread reconfigure the amp, DAC mux and ramp
underneath us (`EXTAMP Enable=0`, `Audio_DacMux_Set()`, `set_ignore_ramp`).
Removal produced no such reaction — only our own `tinymix`. Worth knowing
before blaming our code for codec state changing without us. **The speaker is card 0 device
23**; the mic is 24. Checking `pcm0p` reads `closed` and proves nothing — that
is Android's own device, and mistaking it for ours cost a wrong conclusion.
On timeout it opens anyway, which is the pre-existing behaviour: the wait is
there to make the common case work and to leave a log line naming the holder.

**The log looks healthy while this happens**, which is most of why it went
undiagnosed: `[mic] clock` lines keep appearing every 60s from a goroutine
started before the block. The tell is what is *missing* — `PcmSpeaker
initialised` never appears.

**Unplugging left the speaker silent until the next reboot.** accdet mutes
`Ext_Speaker_Amp_Switch` on insert, which is correct — the Dot should not also
play to the room — and nothing ever turned it back on. `Init()` was the only
thing that set it, which is exactly why a reboot appeared to fix it.
`internal/bindings/jack` watches `h2w` and re-enables the amp on removal.
Insert needs nothing from us.

**Routing is PHYSICAL and needs no code.** The jack's own switch contacts
divert the signal. A voice response was heard in headphones while the mixer
still read `Ext_Speaker_Amp_Switch=On`, `Ext_Headphone_Amp_Switch=Off` and
`Headphone_Speaker_Mux=Speaker` — so those controls do **not** describe where
audio goes, and driving them would be wrong. Do not build a routing layer.

**Unresolved, and it is not only on removal (#117, #141).** A plug in the
jack degrades the whole audio subsystem for as long as it is present.
Characterised 2026-08-12 on Office; still no mechanism.

- **Mic capture stalls on a ~102.3s metronome.** 27 measured gaps at
  102.3s ±2s, every outlier an exact 2× or 3× (a sub-threshold tick), each
  costing 0.3–2.3s of capture. Zero stalls in 51h on an unplugged Retreat
  and 21h on Lounge, against 67 in three hours on a plugged Office. Healthy
  devices run a slightly NEGATIVE `[mic] clock` deficit; a plugged one runs
  positive. It arms some minutes after an insert — measured at 3 and at 14,
  so **wait the full window before reporting it absent** — and stops within
  one cycle of a removal.
- **Audio output dies**, and recovers instantly on removal. Three different
  audible outcomes were observed with a plug in — both speakers at once,
  neither, and headphones-only — and it keeps cycling between them.
- Downstream, `OWW: no mic frames for 10s` eventually breaches
  `ping_timeout` (10s) and the controller tears down the ESPHome satellite,
  BLE proxy and data plane. **That teardown is what users report**: the HA
  media_player entity dying and rebuilding reads as music pausing,
  restarting and skipping (#141).

**Everything programmable is exonerated, by measurement rather than
argument** — do not go looking here again:

- all **6016** tlv320aic32x4 registers (`regmap/2-0018`) identical between
  audible and silent, on a volume-matched diff
- all **218** MediaTek SoC audio registers
  (`/sys/kernel/debug/mtksocaudio`, `mtksocanaaudio`) identical but for
  free-running counters (`AFE_DL1_CUR` DMA pointer, `AFE_IRQ1_CNT_MON`,
  `AFE_IRQ_STATUS`, `AFE_MEMIF_MON0`, `AFE_ADDA_SRC_DEBUG_MON0`)
- the full ALSA mixer identical — **including across all three audible
  outcomes above**, which is what proves output routing is not under
  software control
- the audio content itself clean: `[aec] far` reports `mean≈0` and a crest
  factor of ~13dB throughout, so we are writing well-formed audio, not DC
  and not garbage

**It is load-independent.** A 3-pole cable, a 4-pole cable and headphones
all fail. That killed both load-specific theories (a CTIA/OMTP wiring
mismatch, and a capless HP driver into a grounded line input — a floating
load is immune to the latter). `accdet_amzn` reports `Headset_plug_in` for
**all three**, so it cannot tell them apart.

Two traps for whoever picks this up:

- **`Ext_Speaker_Amp_Switch` does not gate the internal speaker.** It was
  `Off` while the internal speaker was audibly playing. accdet also mutes
  it on insert only *sometimes*. Making that deterministic ourselves looks
  like an obvious fix and is currently the wrong one: accdet failing to
  mute is the only reason a user hears anything at all with a plug in, so
  it would turn "wrong speaker" into "no sound".
- The mic stall log line says "ALSA overrun", which is an interpretation.
  It measures the arrival gap in `readLoop`, and the GoTinyAlsa stream
  channel is 16 batches (2.56s) deep, so a stall of that goroutine looks
  the same. The growing clock deficit does show audio is genuinely lost.

**ANSWERED 2026-08-12: the hardware is fine and this is ours.** The same
external speaker and cable, plugged into a **stock unlocked Dot still
running Alexa**, works correctly. Two EchoMuse devices fail (Office, and
#141's reporter on their own hardware); one stock device works. The common
factor is our software.

That also retires the worn-connector theory, which never had support —
#141 already meant a single bad connector would require two independent
units to fail identically. Do not spend time on a second-EchoMuse-device
comparison; it can only restate what #141 already says.

The live hypothesis is therefore the HAL: on stock, Amazon's audio HAL and
mediaserver own the codec and coordinate with accdet on jack transitions.
We `stop media`, take `pcm23p` and drive the codec ourselves, so accdet
fires and the thing meant to respond is not there — and a retry that never
completes looks exactly like a 102.3s metronome. Note `stop media` does
NOT stick (mediaserver restarts); what we hold is the PCM.

Two experiments left, in order:

1. **Diff a stock unit's state against ours, plug in and audio playing** —
   `tinymix`, `/sys/kernel/debug/mtksocaudio`, `mtksocanaaudio`,
   `regmap/2-0018` and `dmesg | grep accdet`. Same driver, same hardware,
   different userspace. A control the HAL sets on insert and we leave
   alone would be the fix.

   **`h2w` is NOT the difference** — a stock unit reads `1`
   (`Headset_plug_in`) for the same cable that reads `1` on ours. The
   kernel sees identical state on both stacks, so detection is not where
   this diverges; the divergence is entirely in what userspace does in
   response. That makes the fix more likely to be an action the HAL takes
   on insert than a fact it knows.
2. **`/proc/interrupts` sampled across several cycles** with a plug in. A
   metronome has a source; an IRQ that ticks only on the beat names it.

**Stereo is not supported and the device end is not the blocker.** ALSA is
already opened with two channels and `PumpPeriod` duplicates L=R; the mono
downmix happens at the controller, in three ffmpeg calls (`-ac 1`). That was
right when a mono internal speaker was the only output. With a jack it throws
information away — see the stereo issue rather than reinventing the analysis.

**`tinymix` IS on these devices** (`/system/bin/tinymix`), and the codec
regmap is readable at `/sys/kernel/debug/regmap/2-0018/registers`
(`tlv320aic32x4`). Do not drive `tinyplay` while the server is running: it
contends for the same PCM and wedged a device hard enough to need a power
cycle.

## CPU topology, thermals and why `cpuPct` lies

The MT8163 is a **quad-core** Cortex-A53 (`/sys/devices/system/cpu/present` =
`0-3`) and MediaTek's hotplug strategy parks all but cpu0 when idle. So
`/proc/cpuinfo` showing one processor is a **power state, not a limit** — a
mistake worth not making twice, because it turns a comfortable measurement into
an apparent ceiling.

HPS (`/proc/hps/`) governs it: `up_threshold=80` / `up_times=2` bring another
core online after two samples above 80% utilisation, `down_threshold=70` /
`down_times=20` park it again (slowly), `rush_boost_threshold=98`,
`input_boost_cpu_num=2` boosts on button presses. cpu0 runs at 1.3GHz — its
maximum — under the `interactive` governor, so no frequency headroom is being
withheld. The `num_limit_*` files are ceilings (all 4 = nothing capping);
**`num_base_perf_serv` is the FLOOR**, and the firmware raises it to 2 at
startup (`applyCoreFloor`). That is deliberate: the mic pipeline has a hard
160ms deadline and now shares a core with wake word inference running in ~31ms
bursts, and a floor of 2 lets them run in parallel instead of relying on
hotplug reacting to a burst that has already begun. It is procfs, so it does
not survive a reboot — hence applying it in the binary, which re-applies every
start. Do NOT write `cpu1/online` directly: HPS re-parks it within
`down_times`, giving a setting that appears to work and silently stops.

**`cpuPct` is a share of ONLINE capacity**, derived from the aggregate
`/proc/stat` line. The same absolute work therefore reads as *half* the
percentage once a second core comes up — measured on Lounge, 51% on one core
became 25.5% on two with the workload unchanged. Always read it next to
`coresOnline`; a `cpu_avg` series without the core count can show a "drop" that
is purely a change of divisor. That is why both are reported and persisted.

Thermals: 11 zones. `mtktscpu` is the CPU/SoC (reported as `cpuTempC`),
`mtktspmic` the PMIC and `tmp103` a discrete board sensor; `maxTempC` is the
hottest of all of them, because trouble does not always appear on the zone you
thought to watch. Idle sits at 31–34°C, nowhere near throttling.
**`thermalCoreLimit` (`num_limit_thermal`) is the sharpest throttling signal
this SoC offers** — below `coresTotal` means the governor is already capping
capacity, which bites well before any temperature reading looks alarming.

## Persistent activity stats

Every voice turn is persisted to SQLite at completion (`turns` table, `db.insert_turn` from `em_esphome`): trigger, wake model/score/threshold, room noise floor at detection, outcome, STT text, stage latencies, and playback underruns.

**Delivery instrumentation (schema v7, firmware v2.9.6+).** Underruns are rare and binary; these measure the *margin* on every stream so degradation is visible before it's audible. Device-reported in `playback_stats`: `min_depth` (fewest periods left in the device buffer mid-stream — the headline number), `prime_wait_ms`, `recv_span_ms` (first→last frame arrival; longer than the audio duration means delivery was slower than realtime), `max_gap_ms`, `bytes_recv`. Controller-measured: `send_ms`, `delivery_ms` (first frame sent → device's `playback_stats` arrival), `eq_ms`. **`send_ms` is a socket-write time and completes near-instantly however slow the link is — never read it as delivery; that mistake cost a whole investigation on 2026-07-20.** `device_metrics` gained link context (`link_speed_last/min`, `wifi_freq_last`, `wifi_bssid_last`, tx/rx byte and error sums) — band and BSSID matter because one SSID spanning 2.4/5GHz lets a device silently re-associate to a much slower radio. `event_loop_lag_monitor` tracks controller-side stalls (peak on `/api/system/status` as `loop_lag_peak_ms`); anything blocking the loop also delays speaker frames. The underrun count arrives asynchronously — the device reports `playback_stats` (periods + underruns) once per completed speaker stream, and the controller attaches it to `device.last_turn_id` (consumed on use so an announcement's report can't overwrite a turn's stats; NULL underruns = never reported, e.g. pre-v2.9 firmware). Two hourly rollup tables ride alongside: `wake_counters` (near-miss counts/max score, flushed through the existing 2s-rate-limited near-miss path; plus non-turn underruns) and `device_metrics` (CPU/RAM/storage/RSSI sums+extremes upserted per ~30s device stats report — averages computed at read). `Device.turn_history` is hydrated from `turns` on connect, so the dashboard Activity tab survives restarts. Read APIs: `/api/devices/{id}/turns` (raw, `limit`/`since`) and `/api/devices/{id}/activity?days=N` (per-day aggregates, per-wake-model rollups, counters, metrics — plot-ready). Keep instrumentation at this cost class: one insert per turn, one upsert per 30s/2s — nothing per audio frame. The v7 device counters honour this: per-period work is one `len(chan)` compare plus one `time.Now()` on a single-writer path (no locks, no allocation, no logging), all of it emitted on the *existing* `playback_stats` message. `wpa_cli` is the one exception that costs a process spawn, so `linkInfo()` caches it for 2 minutes rather than running per stats tick.

**Control-plane RTT (schema v9/v10).** The RF layer is OPAQUE on this hardware and its counters are worthless: the MTK driver leaves retry/discard/missed-beacon at zero in `/proc/net/wireless` whatever the link is doing, reports `NOISE=9999`, and there is no `iw` binary — so `tx_errors`/`tx_dropped`/`rx_crc` are STRUCTURALLY zero and `get_device_metrics` deliberately does not surface them (a zero there reads as "healthy link" and is not). RTT is the latency signal that works: the controller stamps each control-plane `ping` with a sequence id (every `PING_INTERVAL_SEC`=5s), the device echoes it, and RTT is computed against one monotonic clock — the device never stamps its own, because Echos boot with bogus clocks pre-NTP. Unsolicited keepalive pongs carry no id and are ignored rather than paired with whatever ping is outstanding. Samples aggregate in memory (`Device.record_rtt`/`drain_rtt`) and flush on the existing ~30s stats report, so the DB cost is unchanged; note this means **adding an RTT field needs `drain_rtt` updated as well as `record_device_stats`** — the relay guard in `tests/test_db_instrumentation.py` covers both sources. Excursions (≥`RTT_EXCURSION_MS`=200) are split by whether the device was busy at SEND time, and `rtt_samples_idle` is the denominator that makes the split meaningful: without it "every excursion was idle" is vacuous, since almost every sample is idle. Read API exposes per-state RATES, never raw counts. **ROOT-CAUSED 2026-08-11 (#139): the link is fast and LOSSY, and the
excursions are TCP retransmission delays rather than latency.** ICMP from the
device to the controller measures p50 5.5ms / max 16.8ms with **zero** samples
over 200ms, in the same 240s window that the app RTT threw 13 excursions up to
1356ms. What ICMP does show is **4.6-7.1% packet loss**, and `ss -tin` on the
device sockets shows the consequence: 4308 retransmissions on one socket,
14.4% of its bytes retransmitted, TCP's smoothed RTT reading 84-119ms against
the real 6ms, and **RTO driven to 500-800ms**. That is where the excursions
come from, and they cluster accordingly (400-700ms and 1000-1400ms).

Three things follow, and the third is the one that bites:
- **RSSI does not order the results.** Main Bedroom threw 15.7% excursions at
  **−38dBm / 150Mbps**; Lounge has the worst signal of the original three
  (−71dBm) and the fewest excursions. Nor does load — six connected devices
  produced a *lower* aggregate rate than three.
- **The measurement has to be driven from the device.** These Dots drop all
  unsolicited inbound: 1270/1270 ICMP lost over 22 minutes, and a TCP SYN to a
  closed port gets no RST either. Ping *out* from the Dot instead.
- **WebSocket rides TCP, and TCP is ordered**, so one lost segment blocks
  everything behind it. This is what turns a 6ms link into 1.4s application
  stalls — a measured 1373ms gap before a turn's first audio frame, and a
  1466ms gap mid-playback that drained the device buffer to `min_depth=0`.
  The device is not at fault in either: `[mic] clock: stalls=0` throughout.

The architectural response is #140 (assume 5-10% loss and 1-2s outages;
`tc netem` test mode). **Do not attribute recording artefacts to this** — TCP
does not lose data, so a stall delivers late, never never, and cannot punch
holes in a saved utterance. That mistake was made and corrected on the day.

**Utterance recordings (schema v12).** Opt-in per device via `saveUtterances` (Config → Microphones): the mic audio streamed to HA for a turn is kept as a 16kHz mono WAV in `recordings/` beside the DB, playable and downloadable from each turn's row in the Activity tab (`GET /api/devices/{id}/turns/{turn}/audio`). Lets you hear what STT heard instead of inferring it from a bad transcript. Buffered in `_stream_mic_audio` **below the denoiser**, so the file is byte-for-byte the ESPHome wire payload — it first shipped tapped pre-NS, which answered "how good is the mic" but could not answer "why was the transcript wrong" on any device with `nsAsr` on, and that is the question people actually ask. **Keep the tap below NS**; if a raw comparison is ever wanted it belongs as a *second* file, not by moving this one. Capped at `MAX_UTTERANCE_BYTES` (30s), written in `_persist_turn` because the filename is keyed on the turn's rowid. Retention is a hard per-device **file count** (`em_recordings.KEEP_PER_DEVICE`=10) — much shorter than `TURN_RETENTION`, so **a non-NULL `audio_file` on an older row is a claim to check, not to trust**; every reader goes through `em_recordings.resolve`, which also re-checks that the file belongs to the device in the URL (the endpoint takes both from the path) and treats a missing file as an ordinary 404. Default OFF and it should stay that way: this is the only feature that writes recognisable speech to disk. `db.delete_device` unlinks a device's recordings explicitly — nothing cascades to the filesystem. Note the dashboard fetches the WAV via `API.blob` rather than an `<a href>`: sessions are Bearer-header-only, no cookie is ever set, so browser-initiated requests would 401.

## Support bundles (`em_support.py`)

`GET /api/support/bundle` (admin) produces one JSON file for attaching to a
public issue — built because remote diagnosis was costing days per round trip
(#62 could not be answered without knowing which entity a user's pause reached).

**It is an ALLOWLIST and must stay one.** Fields are named individually, so a
new database column is excluded until someone deliberately adds it: the
failure mode is that support loses a field, never that user data reaches a
public issue. A denylist gets this wrong once and it is unrecoverable.

Three rules, enforced by `tests/test_support.py`, which asserts secret values
appear **nowhere in the serialised output** rather than checking field-by-field
— a leak through a log line or nested config is the one nobody predicts:

1. **No speech, and no opt-in for it.** `stt_text` and recordings are out.
   `build()` must not grow a `transcripts` parameter; a test asserts that,
   because a flag is a thing people tick.
2. **No user-authored free text.** Device labels routinely contain names
   ("Bedroom - Sam"), so they are replaced with positional pseudonyms.
3. **No network identifiers.** SSID, BSSID and IP are excluded — an SSID is
   geolocatable from public wardriving databases.
4. **No account names.** Dashboard logins reach a bundle through ordinary log
   prose — `Shell session opened by wil` — which is not quoted, not a URL and
   has no identifier shape, so every other rule passed it through (found by
   Wil in a real bundle, 2026-08-02). The names come from the **user table**,
   never a pattern, and are replaced longest-first: with `wil` and
   `wilbowes`, the short one first leaves `<admin>bowes`. A name becomes its
   **role** (`<admin>`), not a positional alias — this is a single-operator
   system, so `user-1` would be one-to-one with a real person, and the role
   is the diagnostic content anyway. The role is validated before it is
   published; it comes from a database column. This is also why the
   controller's own stats report **sizes and never paths** — a data directory
   is `/home/<name>/…` on a bare-metal install.

**Controller CPU is reported over 1m/5m/1h, not just a lifetime average**
(`em_support.CpuHistory`), because a lifetime figure cannot tell a controller
busy *now* from one that was busy for an hour this morning, and those want
opposite investigations. Percent of ONE core, as `top` reports it — over 100%
is a real reading, not a bug. Sampled on the **existing** event-loop lag
ticker (one `os.times()` per 30s, ring bounded to the longest window); do not
give it a task of its own. A window with less than half its span of history
is **omitted rather than extrapolated** — 40s of data reported as `cpu_pct_1h`
is a wrong answer, a missing key is visibly missing.

**An allowlist naming a key nothing produces fails silently and still looks
careful.** `_METRIC_FIELDS` listed the `device_metrics` *column* names while
`db.get_device_metrics` resolves its sums into averages at read, so every
bundle shipped with no device CPU or memory figure at all — most of the
reason to include metrics. Two guards now: `test_metric_fields_match_what_the
_reader_returns` diffs the allowlist against the reader's own source, and a
deploy-shape test pins that the handler attaches `device_id` to each metrics
row (the reader does not, so the fleet's hours pooled into one anonymous
list). Both were verified by reintroducing the bug.

Log lines are **sanitised, never passed through**: quoted strings and URLs are
replaced, and lines from transcript-bearing sources (`STT result`, `text=`,
`Utterance saved`) are dropped whole rather than edited, since partially
redacting a line that quotes a transcript is a bet on a regex. Order matters —
quotes are substituted before URLs, or the URL pattern eats the closing quote
and leaves the line malformed.

**Two log sources, and the distinction is load-bearing** (bundle `format` 2).
`controller_log_tail` is the controller's OWN log, held in a bounded
in-memory ring (`em_support.LogRing`, installed by `em_controller` at
`basicConfig` time); `device_log_tail` is the relayed per-device
`device_logs` table. Until 2026-08-02 only the second existed and it was
named as if it were the first — so every line that would have explained #62
(media state pushed to HA, the ESPHome command flow, barge-in decisions) was
in neither, because it goes to stdout. **A bundle that cannot answer the
issue it was built for is the failure mode to watch for here**, not a
missing field.

Both are sized against measurement, not intuition:
- The ring **drops `aiohttp.access`** (65% of a measured 38 lines/min — the
  dashboard polling itself) and holds 2000 lines, covering a couple of
  hours. At 600 lines including access logs it covered sixteen minutes: a
  ring that reliably contains everything except the event someone opened a
  bundle to report.
- Device lines are **thinned, not truncated**: `[mem]` heap dumps were 89% of
  that table and 87% of a real bundle's tail. `thin_noise` keeps the newest
  three per device and drops the rest — kept rather than dropped outright
  because goroutine count is recorded nowhere else, so a leak hunt would
  lose its only source. Measured effect: 339 lines of which 35 were evidence
  became 195 lines of which 179 are.

Serials ARE included: nothing correlates without them, and they identify the
user's own hardware to them. User-facing contract: `docs/support-bundle.md`.

## OTA update system

**When an update fails, the device explains itself.** `start_server.sh` logs
its decisions to `/tmp/server.log`, which is RAM-backed — so the power cycle
used to recover a device that never came back wipes exactly the lines that
would explain it (2026-08-01, still unexplained as a direct result). The
supervisor therefore ALSO writes its own decisions — boot slot, start, exit
with runtime and code, each fast-exit, the rollback, and **why it is exiting**
— to `/data/local/etc/echomuse/supervisor.log` (`em_api.SUPERVISOR_LOG`; a
test pins the two paths together). Bounded at 64KB with the trim **before**
the append, so a crash-loop cannot outrun it. Timestamps are seconds since
boot, not wall clock, for the usual reason. The controller cannot fetch it at
failure time — the device being gone IS the failure — so both failure paths
record that an explanation is owed and the next successful connect collects
it into the device's log events. Takes effect on the next device reboot after
the script syncs.


The device runs an A/B slot binary system:
- `/data/local/bin/server` is a symlink to either `server_a` or `server_b`
- `start_server.sh` counts fast exits (< 15s runtime); after 3 consecutive failures it flips the symlink to the other slot and exits, letting Android init restart with the fallback binary

OTA is triggered from the dashboard — the controller pushes the new binary via the `/shell` WebSocket.

**md5 decides whether a transfer succeeded, not the shell's exit status.**
`TRANSFER_OK` only ever proved that the base64 decode pipeline and `chmod`
exited 0 — never that the bytes on the device match the bytes sent. Bytes
therefore land in `{dest}.part` and are renamed only on an md5 match
(`_stream_file_to_device`), the same discipline the asset path has always
had. **The point is the ordering, not the error message**: a corrupt binary
and a genuinely broken one produce the *same* observable — three fast exits,
a symlink flip, a device back on its old version — so an unverified transfer
costs a reboot and a rollback to arrive at the same place with less
information, and the mismatch must be caught while the device is still
running happily on its current slot. A failed verification must never reach
the `ln -sf`; `tests/test_deploy.py` pins that ordering.

**A transfer must never delete its destination before sending.** For firmware
the destination IS the rollback slot, so an opening `rm -f {dest}` meant every
failed OTA left a good active slot beside an empty partner — and a later
crash-loop then flips the symlink onto nothing. It also contradicted the
message the user was shown, which promised the slot was left untouched. The
`.part` discipline protects `dest` from a *corrupt* transfer; it cannot
protect it from being removed before the transfer starts (#121).

**A failed transfer names the STAGE it reached** (`TransferResult`, truthy so
existing call sites are unchanged). One message covered five outcomes, and the
two furthest apart — "arrived corrupt" and "no byte was ever sent" — read
identically; #121 was the second reported in the language of the first, three
Dots failing 15s after starting, which is far too fast to have attempted 10MB.
Note a device shell that answers nothing must report as a **link** problem,
never as "no base64 decoder": one is worth retrying, the other is a property
of the device that retrying cannot change.

Three things not to undo:
- Verification rides the **same shell session** as the transfer, and the md5
  tool is detected alongside the base64 decoder in the round trip that was
  already happening — so it costs a round trip on an open socket, not a
  session.
- **No `cut`.** `md5sum` prints `<hash>  <path>`, and the branch where
  busybox is absent is exactly the branch where `busybox cut` is absent too.
  A `case` glob needs no external tool.
- `require_verify` separates "no md5 tool on this device" from "md5 did not
  match". Callers default to accepting the former with a warning — their
  prior behaviour, and the base64 detection already treats a busybox-less
  device as a contemplated state. **Firmware passes True and refuses**, being
  the payload we are about to boot.

Free space is checked before anything is written, via
`em_oww_assets.parse_free_mb` — never an awk field index, for the busybox
line-wrap reason documented in the asset section. An unreadable `df` reads as
**carry on**: it is not evidence of a full disk, and refusing on it would
block updates on any device whose `df` we have not seen. Note binary growth
is not a plausible cause of a space failure here — v2.9.8 is 10.1MB and
v2.10.0 is 10.3MB.

**The release binary is cached on disk** (`em_firmware.py`, `firmware/` beside
the DB). `_fetch_binary` used to re-download the whole ~10MB asset per call, so
a fleet update pulled it once per device and the provisioning wizard again per
device set up; a published tag never changes what it points at. Two rules, both
of which read as over-caution until they are not:

- **md5 decides a hit, not the file existing.** A truncated download leaves a
  file of plausible size, and the OTA's device-side verification *cannot* catch
  it — that check confirms the device received what the controller SENT, so a
  corrupt entry verifies perfectly all the way onto the device, where a corrupt
  binary and a genuinely broken one produce the same observable (three fast
  exits, a rollback).
- **A cache failure is never an update failure.** Every path degrades to "use
  the bytes we already have". This is the *opposite* of the DB-backup-before-
  migration rule, deliberately: there refusing is the safe action, here it
  costs the user the thing they asked for and protects nothing.

Note `Path.with_suffix` is unusable for these names and the first version used
it: a tag contains dots, so pathlib reads `server-v2.11.0` as stem
`server-v2.11` with suffix `.0`, and `with_suffix(".md5")` silently writes a
digest filename that never matches its payload — every read a miss, the cache
doing nothing, and nothing saying so.

Device-side payloads the controller distributes (`start_server.sh` via `/api/provision/start_script`; the debloat pair `debloat_packages.txt`/`echomuse-debloat.sh` via `/api/provision/debloat_packages`+`debloat_script`, applied by the wizard's Debloat step — pm hide list + Magisk service.d daemon stops) live canonically in `controller/device_payloads/` and are read from disk per request — never embed copies in `em_api.py` or `dashboard.jsx`. `device/scripts/start_server.sh` is a symlink into that directory. Every firmware OTA also syncs the device's `/data/local/bin/start_server.sh` against the canonical payload (`_sync_start_script` — md5 compare, heredoc push, rename into place; takes effect on next device reboot), so script drift heals fleet-wide without a separate update path.

**Every payload needs an update path, and `tests/test_deploy.py` enforces it** (a file in `device_payloads/` unreferenced by `em_api.py` fails CI). The debloat pair had none until 2026-07-30 and every fielded device needed a manual push. `_sync_debloat` also rides the OTA and reconciles **both** halves — the boot script by md5, and the `pm hide` list by asking the device which listed packages are still visible — because round 2 added a *package* and a script-only sync would have looked like it worked while changing nothing. It is additionally exposed as `POST /api/devices/{id}/debloat` (Updates tab → Maintenance), which is **required, not a convenience**: the OTA path cannot reach a device already on the latest firmware. Two traps in that reconcile, both of which produced confident wrong answers: match package names with `grep -qx` (whole line) — an unanchored `*package:$p*` also matches `package:$p.client` — and never treat `pm list packages -u` minus `pm list packages` as the hidden count, since it includes uninstalled packages.

`com.amazon.whad` is `PERSISTENT`: `pm disable` is ignored, **`am force-stop` is a no-op**, and `pm hide` does not stop a running instance — it stays until the next reboot, which is why the log line says so. Note RSS overstates the win ~6x (shared zygote pages): the measured recovery is ~20-35MB per device by `memUsedMb`, not the 62MB RSS suggests.

## Provisioning wizard (`dashboard.jsx`, `_WIZARD_STEPS`)

The WebUSB/ADB wizard that takes a stock Dot to a fielded device. Four rules,
each of which has already been broken once and each of which fails *silently*
when broken — the wizard drives hardware nobody is watching a log of.

- **The finishing reboot belongs to the LAST step, and moving the last step
  means moving the reboot.** Install EchoMuse used to be last, so it ended by
  rebooting and clearing `adb`. When the wake word asset step was appended
  after it, that step's auto-run gate (`&& adb`) was false, so it never fired
  once: no error, no log line, no button, just a wizard sitting on a step
  against a device that had already rebooted away. An auto step reached with
  no connection now marks itself failed and says why.
- **A step must never report success having achieved nothing.** A run once
  reached Configure WiFi having disabled 0 of 11 Alexa packages and hidden 0
  of 32, both steps green. Zero successes is a package manager that was not
  working, not an unusual SKU — and continuing to WiFi with the Alexa stack
  live is the outcome most worth failing loudly to prevent.
- **`pm` not being ready has TWO error shapes.** Before PackageManagerService
  is published you get the friendly `Could not access the Package Manager`;
  once it IS published but not yet initialised you get
  `NullPointerException: ... ArrayList.size() on a null object reference`
  straight out of the binder call. Matching only the first is why the retry
  path never fired. `_pmNotReady` matches both.
- **The operator may click Reconnect the instant the device appears in the
  USB picker; working out when Android is ready is the wizard's job.** adbd
  and magiskd both come up long before the framework — `su -c id` returning
  root in 0.4s says nothing about `pm`. `waitForFramework` polls
  `sys.boot_completed` *and* probes `pm path android` (the flag is necessary,
  not sufficient), budgets 10 minutes, and **throws** on timeout. Measured
  boots take ~86s, so the 30s poll it replaced was never enough. No step may
  require the operator to have guessed a long enough wait.

Three more rules, all learned on 2026-08-08 by pulling a cable at the wrong
moment:

- **A step that loses the device does not fail. It HANGS.** The ADB calls do
  not reject when the device goes away: `shell` waits on a stream reader that
  never produces. `running` stays true, and every control in the panel is
  gated on `!running`, so the wizard sits there looking busy with no way out
  but a page reload. A `navigator.usb` `disconnect` listener abandons the step;
  an epoch counter makes the step's own later completion a no-op. Do NOT
  replace this with a timeout on `shell`: `waitForFramework` budgets ten
  minutes and `twrp install` takes thirty seconds, so any timeout loose enough
  to be safe is useless. Note the in-flight transfer can ALSO throw first; the
  two race, so both paths take the same exit.
- **Pulling the cable powers the Dot off.** Micro-USB carries power and data,
  and `reboot recovery` is a one-shot BCB flag, so a replug is a cold boot into
  **Android** whatever phase the wizard thinks it is in. `_STEP_MODE` records
  which mode each step needs and Reconnect says so when they disagree. This is
  not cosmetic: in Android `/dev/block/other-boot` points at the unlock
  payload, so retrying Patch Boot Image there would destroy the unlock.
- **`Unknown package` is pm ANSWERING, not pm refusing.** It is an
  `IllegalArgumentException` meaning the package is not installed. Counting
  only successes made "this build lacks these packages" and "the package
  manager is broken" identical, and the advice for the second (wait and retry)
  can never fix the first, so a device on such an image could never finish the
  wizard (#91). `_pmVerdict` returns disabled / absent / rejected, and only a
  rejection stops the run. Anything unrecognised counts as rejected, because
  the cost of being wrong is continuing to WiFi with the Alexa stack live.

Two device behaviours the wizard works around rather than fixes:

- **Amazon's OOBE cannot be stopped in time.** It announces itself and spins
  an amber ring the moment the framework is up (~86s), and the earliest root
  lands is magiskd attaching (~74s later, measured) — so Disable Alexa is
  structurally too late, and `pm hide` in Debloat is later still and does not
  stop a running instance. The speaker is muted instead, right after the
  framework answers, with `input keyevent 25` — shell user only, no root.
  Keyevents rather than a volume API: `service call audio` needs a
  transaction number that differs per release, and
  `settings put system volume_music` is not read live by AudioService. Safe
  to leave muted — EchoMuse drives the codec and seeds `startupVolume` after
  the final reboot. **It does not reliably work.** Observed 2026-08-08: she
  talks regardless, either raising the volume back or playing on a stream
  `keyevent 25` does not address (25 adjusts whichever stream is ACTIVE).
  `dumpsys audio` while she is talking would settle which. Left in because
  turning the volume down costs nothing, but the log line says what was done,
  not what was achieved.
- **`SmartHomeWifid` cannot be killed, only stopped.** It rewrites
  `wpa_supplicant.conf`, and `kill -9` does not hold because it is an init
  service — init restarts it and the re-check finds a fresh pid. Read the
  service name out of `init.svc.*` at runtime (init's own record, so it
  survives the name differing across SKUs), `stop` it, then kill. Its
  presence is not cosmetic: a run with it running spent 9s cycling
  DISCONNECTED/SCANNING before associating, against 1s on a clean one.

### The one partition the wizard writes

Patch Boot Image is the only partition write in the whole wizard, and the only
point at which it could reach below FireOS. EchoMuse writes the FireOS kernel
and userspace; it does not write the preloader, LK, amonet's unlock payload or
TWRP. `docs/rooting.md` states that boundary for users.

**The by-name map differs between TWRP and Android**, measured on hardware:

```
                 TWRP    Android
  boot_a          p10        p17
  boot_a_x        p10        p10
  boot_a_amonet   p17          -
```

TWRP remaps the bare names onto the KERNEL partitions and exposes the payload
explicitly as `*_amonet`. So a rule written against one map is INVERTED in the
other, and `p10` answers to two names at once. The first version of this guard
was written against Android's map and passed anyway, on the accident that the
glob listed the safe alias last. So `classifyBootTarget` matches on
suffix, reads every alias of the target, and has a test that reverses the
probe output.

Around it: `dd`'s stderr reaches the log rather than `/dev/null`, the pulled
image must carry the `ANDROID!` magic before the fixed-offset cmdline patch
runs against it, and the cmdline is read back off the partition afterwards.
Every failure path leaves the device in TWRP and says so.

### Diagnostics when a step fails (`em_support.build_provision_diagnostics`)

On a step failure the wizard runs a fixed read-only probe set, POSTs it raw to
`/api/provision/diagnostics`, and offers the sanitised result as a download.
Collection is automatic; sharing is deliberate.

**Packaged on the controller, never in the browser.** The redaction rules and
their tests live in `em_support.py`; a second copy in JavaScript would drift
until a file carried an SSID. The wizard collects raw and `em_support` decides
what survives, which also treats the browser as untrusted input. That is
correct, since it is talking to a device we know nothing about yet.

It is an allowlist twice over: an unlisted probe name is dropped whole, and
the key/value probes have their keys listed too. `tests/test_support.py` pins
the JS probe list against the Python allowlist. Drift there is silent, since
a probe collected and dropped looks identical to one never asked for.

Scan results are the real tension: the flags and frequency ARE the diagnosis
(`[SAE-CCMP]` is the whole answer to #82) while the names locate someone's
house, so rows survive with the SSID replaced and the selected network marked.

## Device config push

`config.ConfigMessage` JSON fields (camelCase) are sent from controller to device on connect and on per-device config change. Non-zero fields are applied; zero/nil fields are ignored (partial update). Changes take effect immediately — no restart required.

Configurable parameters: `vadThreshold`, `vadSpeechMs`, `vadSilenceMs`, `owwThreshold`, `owwModel`, `owwSpeexNs`, `adcDigitalGain`, `adcMicpga`, `micGainDb`, `startupVolume`, `beamAngle`, `beamformingEnabled`, `aecEnabled`, `aecDelayMs`, `aecTailMs`, `agcEnabled`, `nsAsr`, `bargeInEnabled`, `bargeInThreshold`, `bleProxyEnabled`, `eqBands`, `eqLoudness`, `ledScene`, `ledListenColor`, `ledThinkColor`, `meterAttack`, `meterDecay`, `meterFloor`, `meterGamma`, `meterRef`, `meterCurve`, `wakeArbitrationMs`, `duckDb`, `buttonSingleTapEvent`, `buttonMultiTapMs`, `owwOnDevice` and `saveUtterances` (the last two are controller-consumed for scoping purposes, though `owwOnDevice` IS acted on by the device; `saveUtterances`, `wakeArbitrationMs` and the two `button*` keys are ignored by it).

### Fleet vs device scoping (schema v8)

Scoping is **per section**, not one boolean. `em_config_sections.py` is the single source of truth mapping each config key to one of six sections (playback, wakeword, microphones, ring, advanced, bluetooth); `devices.config_sections` stores the set a device overrides, and `get_effective_device_config` = fleet overlaid with the device's values for those sections only. `use_global_config` survives as a derived compat view (no sections == fleet) and must not be treated as authoritative.

Three invariants, each guarded by `tests/test_config_sections.py`:
- **The partition must stay total** — a new key in `DEFAULT_DEVICE_CONFIG` that belongs to no section can never be overridden and never renders. Add the key to a section in the same change.
- **`dashboard.jsx`'s `CONFIG_SECTIONS` mirror must match Python** — it is parsed as JSON out of the file, so keep it comment-free and double-quoted. Drift puts a control under a toggle that does not govern it.
- **`STATE_KEYS` (`startupVolume`) are never section-scoped** — persisted device state, always taken from the device, never fleet-inherited.

Reverting a section **discards** its stored values (`set_device_config_sections` prunes), so no shadow values resurrect on a later re-override. Both config write paths push the **effective** config via the shared `_apply_live_config`, never the request body — with per-section scoping a body is partial by design, and the fleet endpoint now pushes every connected device rather than only fully-inheriting ones.

### Volume / mute persistence

**The scale stops at the codec's unity gain, and that ceiling is load-bearing.**
tinymix ctl 61 is the tlv320aic32x4 DAC *digital* volume: 176 steps of 0.5dB
spanning −63.5…+24dB, with 0dB at index **127**. The firmware shipped
`volumeMax = 175` — the control's own maximum — so the top 27% of the range
applied up to +24dB of digital gain to already near-full-scale PCM and
saturated inside the DAC. Measured on hardware 2026-08-13 (1kHz at −6dBFS,
recorded through the mic array): THD 1.5% at index 127, 2.3% at 136, **65% at
153, 89% at 170**, with the output level *flat* from 153 upward because it had
stopped being able to get louder, and h3 at −1.1dB relative to the fundamental
(very nearly a square wave). The control that isolates it: index 170 with the
source scaled down to land at the same acoustic level reads 1.1% — clean — so
the gain stage is fine and it is purely source × gain exceeding full scale.
Stock FireOS never writes this control **at all** (absent from
`/system/etc/audio_device.xml` and from every `/system` binary), leaving the
DAC at its 0dB reset default and taking user volume from AudioFlinger's
software attenuation, which only ever attenuates — that is why native Alexa
has no such distortion.

Two things not to undo: `DEVICE_VOLUME_MAX`/`volumeMax` stay at 127 (both
pinned by test), and the conversion lives in **one** place — `em_volume.py`,
because `level / 175` was copy-pasted into `em_controller`, `em_esphome` and
`em_api` with no test on any of them, which is how the wrong ceiling survived.
The lost headroom **cannot** be bought back from `Ext_Amp_Gain` (ctl 13): that
control is inert on this board — sweeping its full 6/12/18/24dB range moves
the output 0.0dB while still reading its new value back, the same shape as the
mute LED being on a different GPIO than Amazon's own HAL believed.
`HP Driver Gain Volume` (ctl 62) *is* live (+18dB commanded → +18.1dB actual,
THD 2.25%) if more output is ever wanted, but that is a taste call to make by
ear, and the speaker's behaviour above stock level is unmeasured.

The **physical buttons** traverse `volumeButtonFloor`(47, −40dB)…127 in 4dB
steps rather than the whole control: the scale is dB-linear, so the bottom
third is indistinguishable from silence and stepping across it spends presses
to go nowhere. Silencing the device is the mute button's job. Explicit `Set()`
calls are deliberately **not** floored — HA's volume 0.0 must still mean
silent — and a press from below the floor lands *on* it, so one press always
reaches audible.

Volume is **state, not a setting** — it rides the config channel but has no dashboard control (the slider was removed 2026-07-25: `SeedVolume` ignores later pushes, so moving it did nothing until the device restarted and any real volume change overwrote it). It is listed in `em_config_sections.STATE_KEYS`, exempt from section scoping, and shown read-only on the Status tab.

Volume persists through reboots **controller-side**: every device `volume_state` report is stored into the device's `startupVolume` config, and the device restores it via `Server.SeedVolume` on the **first config push per run only** (later pushes must not stomp live changes). Until seeded (or a local volume change makes the device authoritative), the device suppresses its connect-time `volume_state` report — reporting the boot-default level is what used to clobber the stored value on reboot. Mute is the opposite: **device-sovereign**, persisted locally in `/data/local/etc/echomuse/state.json` (survives OTA slot flips; written on toggle, restored at boot pre-connect — ADC mute immediately, red ring/button LED after LED init).

## LED priority system

Turn-state ring colours (listening ring, thinking spinner) come from **LED scenes** (`em_scenes.py`), configurable per device (`ledScene` + custom colours). Firmware with the `led_anim` capability (v2.9+) **animates locally**: the controller sends one `led_anim` message per state change ({pattern: solid|spin|rotate|pulse|meter|off, colors, periodMs, ttlSec}) and the device renders frames on its own ticker (`internal/server/animator.go`) — controller/WiFi jitter can't judder the ring. `meter` throbs with the live speaker RMS (tapped at the ALSA write, so it tracks audible audio, not the ~5.5s-ahead send) — measured on the **voice plane only, before the music mix**, unlike the AEC far-end tap which deliberately sees the mixed output; a meter fed the mix throbs to the music bed before the response has started; its response curve is config-tunable (`meter*` keys → `AnimSpec` pointer fields → `resolveMeter`, which clamps independently of the dashboard ranges) because it is a taste parameter that needs iterating in a real room, not a firmware OTA per pass. `ttlSec` is bounded per phase — 30s listening, 135s spinner (**coupled to `_fetch_tts_audio`'s 60s timeout ×2 attempts, since the spinner spans HA think time AND the fetch — move one and move the other**), and computed per response for `meter` via `em_scenes.meter_ttl` so a long TTS cannot self-clear mid-answer. Loss-resilience: newer spec or raw `leds` frame atomically replaces the animation (generation counter), and `ttlSec` is a dead-man that self-clears the ring if the controller dies mid-turn. Legacy firmware falls back to controller-streamed frames. Controller `leds` messages carry an explicit `listening: true` flag on listening-ring frames — the device's direction overlay keys off it (pre-scene firmware inferred "listening" from an all-green ring, which breaks for any other scene; the heuristic remains as fallback for old controllers). The direction overlay brightens the base ring colour instead of painting green. Mute ring (red) and volume arc (cyan) are device-local and scene-independent by design.

Turn *outcomes* are distinguished by rhythm, not colour (red/orange/cyan are taken by mute/link/volume): `no_speech` gets one slow throb, `no_tts`/`tts_error`/`timeout` fast blinks, everything else ends silently. Both ride the existing `pulse` pattern with a 1s TTL so they retire on the device's own ticker — no follow-up message to lose. Driven by `device.last_turn_outcome` (set in `em_esphome._persist_turn`, consumed once by `_leds_turn_end`).

Playback ring clearing waits for the device's `playback_stats` (`device.playback_done`), NOT a wall-clock estimate. The old estimate subtracted socket-write time — which completes near-instantly however slow the wire is — so it cleared the ring up to 6.1s early on exactly the links that needed longest. `playback_stats` is emitted once the audio channel drains after EOS, i.e. the real end of audio; the timeout is only a backstop for the report never arriving.

`server.go` maintains a `ledMode` (direction arc vs. system). System-level LEDs (controller commands, mute ring, pulse animations) always win over the beamformer direction arc. Two paint suppressions in `SetLEDs`/`SetDirectionLEDs` (state is still recorded in `baseLEDs` so the ring can be restored):

- **Mute ring** (solid red) is device-sovereign — enforced since v2.7.8: controller LED writes are recorded but not painted while muted. Needed because muting now terminates an active turn (controller cancels + `speaker_flush` on `mute_state`), so the cancelled turn's LED cleanup arrives after the red ring is up.
- **Volume arc** owns the ring for its 2s display window against *animations* — they repaint ~every 100ms and would otherwise stomp the arc within one frame. It does **not** outrank a deliberate action-button press: a dot release calls `CancelVolumeDisplay()`, which drops the hold so the listening frame paints (it deliberately does not repaint — the controller's frame lands within an RTT, and clearing to black would put a dark gap between the two). The arc is protection from repaint churn, not from the user. On expiry the ring repaints the latest `baseLEDs` frame (`onDisplayExpire` → `paintBaseLEDs`), handing back mid-animation. The arc shows only for physical volume button presses (v2.9.5): remote sets and the boot-time volume seed apply silently (`volumeController.Set` showRing flag). The mute-button LED is sysfs gpio444, active-high — not the gpio445 in Amazon's `libled_hal.so`, whose constant is off by one and whose pad is muxed away (stock drives the pin via the `/dev/mtgpio` ioctl; see `mute_button.go`).

## Dashboard device state

`deviceState()` in `dashboard.jsx` ranks pending / offline / muted / speaking /
thinking / listening / idle, and `_push_device_state` carries all of them. The
trap is that **the flag and the push are separate things and drifted apart**:
pushes existed for listening, thinking and turn end, but nothing pushed the
`speaking` transition, so a turn read listening → thinking → idle and the tile
never showed Speaking at all. It surfaced only when the dashboard's 5s poll of
`/api/devices` happened to land mid-playback, which for a ~2s response usually
did not — so it presented as "stuck on thinking", not "Speaking is broken".

Setting the flag and pushing it are therefore **one operation**
(`Device._set_speaking`), and a test pins that no other assignment to
`self.speaking` exists — a new streaming path cannot reintroduce the gap by
doing only half.

**Which edge is truth, and which is a guess.** `False` is the DEVICE's — the
playback functions wait on its `playback_stats` (sent once the audio channel
drains after EOS) and clear the flag there. Clearing it in the stream task's
`finally` instead drops the tile out of Speaking **seconds** early, because
that returns when the last byte reaches the socket and a socket write completes
near-instantly however slow the link is; the device still has its whole buffer
to play. That is the same mistake the LED ring made until 2026-07-24, in the
same file. `True` is still a controller-side **estimate** — the first period on
the wire — and leads the speaker by up to `SPEAKER_PRIME_SECONDS`, because the
device holds audio until primed. Closing that needs the device to report the
start; `playback_stats` is the only playback message the firmware sends (#203).

**`speaking` and `thinking` are mutually exclusive and starting to speak clears
`thinking`.** Both reach the dashboard and `speaking` outranks `thinking`, so a
stale `thinking` is invisible until speaking clears — and then the tile reads
as if the device started thinking again mid-response. The push is guarded with `except BaseException`, because one
caller is `stream_speaker`'s `finally`, which is also reached when barge-in
cancels the task mid-send; a plain `except Exception` does not catch the
`CancelledError` that arises there, and a dashboard push is not worth failing a
speaker stream over. The assignment is synchronous and always happens.

Note `em_player` must **not** set `device.speaking` for music — it makes the
wake loop drop frames, deafening the device for the length of a song.

## Dashboard styling and theming

`dashboard.jsx` is inline-styled, which for a long time meant its colours
could not be restyled from a stylesheet at all — 126 distinct hexes typed at
389 call sites, with four different reds doing the same job. That is a
correctness problem before a taste one: **a light/dark toggle is impossible
while a value lives at the call site.**

- **Every chrome colour is a token** in `dashboard.html`'s `:root` block, and
  both themes must define the same set. `tests/test_design_tokens.py` enforces
  three things and has *no opinion about how anything looks*: every `var()` is
  defined, the two themes define identical token sets, and literal colours
  cannot increase (a ratchet, so remaining call sites get cleaned a pane at a
  time). The first matters most — **an undefined `var()` renders as nothing**,
  transparent text and invisible borders, and reports no error.
- **Three groups keep literal colours on purpose.** `LedRing` /
  `DeviceDiagram` render the physical Dot and its real LED colours (a device
  drawn in "dark mode" would be a different device); the LED scene swatches in
  `DeviceConfigForm` are values sent to the hardware, not styling; `Shell`'s
  xterm theme is a 16-colour contract programs address by index. `deviceState`
  is the subtle one: `dot` is the simulated LED and stays literal, `color`
  beside it is chrome and is tokenised.
- **Never concatenate hex alpha onto a colour.** `` `${color}88` `` worked
  only while every value was a literal; the moment call sites became
  `var(--lcd-green)` it produced `var(--lcd-green)88`, invalid CSS that drops
  the whole declaration — the LCD glow silently vanished for a release. Use
  `color-mix(in srgb, X 53%, transparent)`, which takes a `var()`.
- **The theme is applied by an inline script in `<head>`**, before first
  paint, not in React — the bundle is ~220KB and loads at the end of `<body>`,
  so a dark-mode user would get a full flash of the light dashboard on every
  navigation. It defaults to the OS preference until the user picks; the pick
  then wins permanently and is stored in `localStorage` under `em-theme`.
- **Dark is not an inversion.** The warm grey is the product's identity, so
  dark is the same hue family taken down past the LCD rather than a neutral
  charcoal. Semantic colours are lifted (`#286040` on a dark ground is
  unreadable), and the inset dark regions — LCD readouts, console, wizard
  transcript — stay dark in *both* themes because they are meant to read as a
  lit panel set into the surface; in dark they go slightly darker still and
  lean on their own border to stay distinguishable from the ground.
- **Sheens and hairlines are tokenised too**, and that is the half that would
  otherwise look broken rather than merely wrong: `rgba(255,255,255,0.7)
  inset` is a highlight on a light card and fog on a dark one. Black drop
  shadows are correct on both grounds and are deliberately left alone.
- **Repeated chrome lives in CSS classes** (`.em-pill` with additive
  `--small/--big/--accent/--danger` variants and `:disabled` handled in CSS so
  it beats every variant; `.em-panel`, `.em-label`, `.em-lcd`, `.em-inset`,
  `.em-console`, `.em-iconbtn`). One-off layout stays inline, next to the
  markup it positions — moving that into CSS trades an inline object for a
  class name plus a rule in another file, which is not an improvement. Note
  inline styles cannot express `:hover` or `:focus-visible` at all, so until
  the class layer existed the dashboard had **no keyboard focus ring
  anywhere**.

## cgo dependency

SpeexDSP C source (AEC) is vendored in `device/internal/aec/`. The compiler Docker image provides the ARM cross-toolchain. If adding new cgo dependencies, they must compile cleanly with the `echomuse-compiler` image against the FireOS 5 sysroot.
