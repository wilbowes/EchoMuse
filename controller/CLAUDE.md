# CLAUDE.md — `controller/`

The Python asyncio controller, its Home Assistant add-on, and the dashboard.
Project-wide direction, the device/controller compatibility rules, the wire
protocol and the release scheme are in the repo-root `CLAUDE.md`; the device
firmware is in `device/CLAUDE.md`.

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

**The ESPHome `mac_address` is a stored IDENTITY, not a derivation and not a
network address.** Nothing routes to it and no packet carries it; HA keys its
device registry on it, and its config flow aborts the zeroconf step with
`mdns_missing_mac` when the TXT record lacks one — so a device advertised
without it never produces a discovery card, silently. The mDNS TXT value and
the `DeviceInfoResponse` value must agree, so the mac is resolved once and
**passed** to `_make_device_mdns_info` rather than derived twice.

It used to be computed from the serial on every call, and that was two bugs in
one. The derivation stripped non-hex characters from an alphanumeric serial, so
devices from one batch differing only in the trailing letters collapsed onto
one address and HA treated two Echoes as one, overwriting the first (#212,
found by @lennart24 in #217). And because identity was a *function*, fixing the
derivation would have moved EVERY device, orphaning every HA device row and the
automations referencing their entities.

`db.get_esphome_mac` assigns once and stores (schema v19), the same
assign-once discipline `get_esphome_port` already uses. The v19 fixup seeds
existing rows with their **current** address wherever it is unique, so nothing
that works today moves; within a colliding group the oldest keeps it and the
rest take the new derivation, since they are the ones being overwritten now.

The new derivation is a **fixed prefix** `02:EC` plus 4 bytes of md5, not a
masked hash. `0x02` is the locally-administered bit — the private-address
equivalent for MACs — and bit 0 is a *different* flag (unicast vs multicast)
that must stay clear; a raw hash sets it half the time and produces something
that is not a valid unicast address. A fixed prefix satisfies both by
construction, so there is no bit for a later change to forget, which is what
Docker (`02:42`) and QEMU (`52:54:00`) do. The cost is 32 bits of hash rather
than 48, affordable because uniqueness is only ever needed within ONE HA
registry — 1.15e-6 at 100 devices.

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
  Bounded to **one turn**, so the `_ha_never_started` path above — a genuine
  `RUN_END` with no `RUN_START`, which stalled the satellite setup dialog when
  it was missed — stays untouched. `RUN_START` releases the barrier and is itself **delivered**, not
  swallowed; eating it would leave `_run_started` False and re-arm the same bug
  for the turn's own terminal `RUN_END`.
- **A barge is not the only way to orphan a run, and the guard belongs at
  teardown.** Any turn that stops waiting while HA is still working leaves the
  run live: a `timeout` gives up after 30s, and `no_speech` never sends the
  `end=True` sentinel at all. Both used to walk away silently, and the stale
  `RUN_END` then landed on the NEXT turn, which had not seen its own
  `RUN_START` — `_ha_never_started` read it as terminal and killed that turn in
  ~3ms. Measured 2026-08-25: every `pipeline_refused` in a 15-hour sample
  followed a timeout, none followed a good turn, so one slow HA intent cost
  three turns and asking again was the guaranteed failure.
  `timeout` was fixed on its own path first (#329) and `no_speech` — the far
  more common one — sat one branch away, so the guard moved to the turn's
  `finally` (#333): `if self._run_started and not self._run_finished:
  end_ha_run()`. That is the invariant, and it covers returns nobody has
  written yet. `_run_finished` is set by both branches where HA genuinely ends
  a run, so an ordinary turn tears down with nothing to do.
  `end_ha_run` is split out of `abort_ha_run` because **teardown is not a
  barge** — letting it default `_turn_end_reason` to "barged" would invent a
  cause on every turn that merely stopped waiting — and it is **idempotent**,
  because a barge ends the run and then teardown runs anyway, and a second
  `start=False` there races the *interrupting* turn's pipeline, which is the
  failure the barrier exists to prevent.

**A low barge threshold needs TWO consecutive frames, and the reason it went
unnoticed is that responses used to be short.** The watcher scores 80ms frames
of the device's own microphone during playback, at a bar ~10x below the wake
threshold (speech over TTS is depressed ~25dB by the echo). It fired on ONE
frame. Measured 2026-08-20 on a conversational agent asked for a story:

| turn | frames scored | peak | outcome |
|---|---|---|---|
| short reply | 353 | 0.029 | fine, under the bar |
| story | 336 | 0.091 | **false barge at 8s** |
| story | 604 | 0.184 | **false barge at 24s** |

Long-form narration scores higher — continuous speech offers far more
phoneme sequences resembling a wake word — and gets hundreds more chances.
Note the shape of the old code: the careful two-consecutive-frame rule
guarded the **thinking** phase, where the threshold is the full wake bar and
nothing is playing, while the phase with the low bar scoring the assistant's
own voice had no debounce at all.

`em_barge.decide` now owns both phases, pure and tested — the suite cannot
import `em_controller`, which is why this shipped untested. `bargeInThreshold`
also moved 0.05 → 0.25, measured against real speech-over-TTS scores of
0.3–0.5. **The two are meant to move together**: the frame rule is what makes
a low bar survivable, and the threshold is what buys margin when it is not.
The old default's comment predicted this exact symptom ("a device cutting its
own response short") and asserted the fleet did not do it — the measurement
had simply never included a long answer.

**Moving the default did not move the fleet, and it took two weeks to notice.**
A support bundle on 2026-08-23 showed `bargeInThreshold: 0.05` still stored in
the fleet config, because a stored value beats a changed default — the rule
already written down under "check the fleet before changing defaults", met
again from the other direction. The consequence was live in the same bundle's
log: a barge fired on scores of **0.072/0.111**, which is noise rather than
speech, and cut the response off. Two consecutive frames is no protection when
the bar is where noise sits. When a default moves for a *safety* reason, read
the live DB and decide explicitly whether fielded devices are migrated —
shipping the new number to new installs only leaves the people who already
hit the bug still hitting it.

**The user-visible damage is bigger than the interruption**, because a false
barge starts a phantom turn that hears nothing and runs 20–46s, and
`oww_paused` covers the whole turn — so the device is deaf throughout. It
reads as "it stopped talking and then ignored me". Bounding that is #195.

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

**Announce-then-listen is the SAME message with field 4 set** (#335, #396).
`assist_satellite.start_conversation` and `assist_satellite.ask_question` both
reach us as `VoiceAssistantAnnounceRequest` with `start_conversation=True`;
`preannounce_media_id` (field 3) is the attention chime, and both were carried
by the vendored protobuf and read by nothing. Four rules:

- **HA filters eligible targets on `VoiceAssistantFeature.START_CONVERSATION`**,
  so without that bit the device does not appear in the action's target picker
  at all — no error, an empty list. It is advertised **per device**, gated on
  the `mic` capability (`_voice_assistant_flags`), which is the only reason the
  rest of `VOICE_ASSISTANT_FLAGS` being a module constant is a gap rather than a
  bug. That gating works only because `set_device_capabilities` bounces the HA
  connection when the set changes: the flags ride `DeviceInfoResponse`, a
  one-shot at connect.
- **Listen AFTER `AnnounceFinished`, not before.** HA blocks on it for the whole
  announcement, and `async_internal_ask_question` arms its answer future only
  once `async_start_conversation` returns.
- **`ask_question` truncates HA's own pipeline at STT** — it sets
  `end_stage = STT`, keyed on that future — so the run ends after `STT_END` with
  **no `INTENT_END` and no TTS**, which the `RUN_END` guard above waits for.
  Without `_answer_only`, every question was answered correctly and then parked
  the device on the 30s TTS wait and recorded a timeout. The flag is derived
  from the trace's own trigger label so it cannot disagree with the stats, and
  the outcome is `answered`, not `no_tts`: the transcript IS the deliverable.
- **A muted device runs the turn anyway, and that is deliberate.**
  `async_internal_ask_question` awaits its answer future with **no timeout**, so
  a satellite that refuses by staying silent hangs the caller's script for good.
  The mute is enforced where it always was — the device rejects every
  `mic_start` while muted and the ADC is muted in hardware — so nothing is
  captured, the streaming phase gives up at its cap, and HA ends the run with no
  answer. Refusing controller-side is the change that looks safer and is worse.

The turn is the button turn with a different label (`CONVERSATION_TRIGGER`,
`preroll_discard=0`, `is_wakeword=False`). It must **not** borrow "button" or a
"wakeword(…)" label — the dashboard groups wake statistics by it, and
`wake_word_phrase` is keyed on the prefix.

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

## Schema migrations

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

## Controller audio pipeline

1. **Wake word** — openwakeword (ONNX) runs in a thread executor per device on `mic_queue`. When 2+ devices are connected, `em_arbiter.py` applies **first-detector-wins** suppression: the first device to cross threshold answers *immediately* (no added latency, the claim is synchronous) and any other device detecting within `wakeArbitrationMs` (default 700, 0 = off) stands down and logs "Wake ceded". The claim is released at turn end. Do NOT reinstate the original best-SNR-after-a-wait design: it taxed every wake ~364ms (it gated on devices *connected*, not in earshot) and field data showed SNR at detection was indistinguishable across devices (0.9/1.15/0.93) while the SNR winner produced a worse transcript than the first detector.

    **A device with no HA behind it stands down BEFORE arbitration, and never runs the turn at all** (`em_esphome.can_serve_turn`, the same `get_server`/`get_satellite` pair `trigger_voice_turn` refuses on, so a device counted as able cannot turn out to be unable a tick later). Detection order is a **proximity** proxy and says nothing about whether HA has ever dialled that device's satellite port, so unqualified first-detector-wins hands the utterance to an unlinked Echo, stands down the linked one, and the winner then dies `no_ha` in milliseconds: nothing answers, and the device that could have is the one that went dark. Measured on the fleet 2026-08-29 — a device scoring **0.912** lost to one scoring 0.609 that crossed 449ms earlier, so loudness and detection order do genuinely disagree; that is one observation and not a case for reopening best-SNR, which stays settled. The ordering is the guard: a check after the claim leaves the claim taken, and `tests/test_deploy.py` pins that `can_serve_turn` precedes `_wake_arbiter.claim` and gates it. `em_arbiter` deliberately does **not** know about any of this — a second copy of the rule is one that can disagree with the first.

    **The stand-down still records the wake and still plays the cue, every time** (`em_esphome.record_dropped_wake` + `_leds_turn_end`). Both matter for the same reason the row exists on the turn path: a wake during an HA outage that leaves no trace is indistinguishable from a device that heard nothing. And the cue reports the **device's state, not a turn's outcome**, so it fires whether or not another Echo took the utterance — gating it on losing would make it vanish exactly on the multi-device fleets where the confusion is worst. That is the correction to the first version of this, which only cued when the device ran its own doomed turn: standing in front of an unlinked Echo, you got a ring that lit and went dark while another room answered, which reads as a broken cue (Wil, 2026-08-29). The button path stands down identically — it is the control someone reaches for when the wake word appeared to do nothing, so silence there is the worst version of the bug.
2. **Voice turn** — on wake or dot-button: drain stale frames → acquire `voice_lock` → stream mic to HA via the ESPHome satellite → receive TTS URL → **incrementally** fetch + ffmpeg-decode straight to 48kHz mono → **EQ → bass guard → limiter** (`em_eq.py`, `em_mbc.py`, `em_limiter.py` — see "The output chain" below for why that order) → stream back as 0x02 frames, **paced to `VOICE_LEAD_S`=4.0s ahead of realtime** (see below). `_stream_tts_audio` pipes the HTTP response into one long-lived ffmpeg and yields PCM as it decodes, so playback starts while HA is still generating — neither the encoded response nor the decoded speech is accumulated. **A retry is only safe before the first PCM has been emitted**; `_fetch_tts_audio` remains for callers that genuinely need the whole buffer

## Pacing: why the voice stream is held 4s ahead, not sent as fast as it can go

**The voice path used to send every period as fast as the socket accepted, and
that cut long responses off mid-sentence.** The chain is mechanical, and every
link is in the code:

1. `stream_speaker_chunks` drains every available period back to back, with TCP
   backpressure as the only brake
2. the device's WebSocket read goroutine calls `PumpPeriod` **inline** per
   `0x02` frame (`data.go`)
3. `pump()` ends in a **blocking** channel send, on a channel `audioChanDepth`
   = 128 periods ≈ **5.5s** deep (`stream.go`)
4. once full, that goroutine blocks inside `PumpPeriod` and stops calling
   `ReadMessage`
5. gorilla fires the pong handler only **inside** `ReadMessage`, so a blocked
   device **cannot answer a keepalive ping**
6. the controller pings every 20s and closes after 10s without a pong
   (`websockets.serve(ping_interval=20, ping_timeout=10)`)
7. the buffer drains at realtime, so the block outlasts the timeout
8. `1011 keepalive ping timeout`, mid-response

Measured on Test Echo 1, 2026-08-30: 3,397,174 bytes — **35.4s of audio sent in
21.3s**, 9.8s of it blocked in socket writes, connection closed five seconds
later. Short responses never reproduce it because they never fill 5.5s, which
is exactly why it read as intermittent for three sessions.

**Sending faster bought nothing.** The device holds ~5.5s and no more;
everything beyond that sat in TCP buffers, which are lost on a reconnect
exactly like audio that was never sent. The excess never improved stall
resilience — it only bought the block.

**`VOICE_LEAD_S` = 4.0 matches `em_player.LEAD_S`**, which reached the same
number from the same constraint for the music plane. Music had been paced since
2026-07-25; voice never was, and that asymmetry was the bug. `tests/
test_voice_pacing.py` guards the lead against `audioChanDepth` **read from the
Go source**, so raising one without the other fails in CI rather than on
hardware.

Three properties keep it safe: a stream behind realtime computes a **zero**
delay (`em_pacing.lead_delay`), so a slow HA or a stalled link is never made
worse; the **first period is exempt** and the lead builds at full speed, so the
prime gate is as prompt as it ever was; and the wait is **raced against
`cancel_event`** rather than a bare sleep, or pacing would add its own latency
to barge-in. `send_ms` deliberately still excludes the pacing wait — it is
documented as socket-write time, and folding a deliberate wait into it would
recreate the misreading that cost an investigation on 2026-07-20.

## The output chain: EQ → bass guard → limiter

Everything the speaker plays runs through three stages in `em_eq.apply` /
`StreamingEQ.process`, in this order, all in float so nothing is quantised
twice. **The order is load-bearing.**

- **EQ** (`em_eq`) — eight bands, static, per device.
- **Bass guard** (`em_mbc`) — a dynamic law below 115Hz. Not a high-pass: it
  removes low frequencies only when they are loud enough to cost real
  excursion, so quiet content keeps its low end.
- **Limiter** (`em_limiter`) — look-ahead peak limiter.

**The chain updates in place while a stream is playing, and is never
rebuilt.** `em_player._feed` used to construct it once per feed, so a setting
only took effect on the next track — which defeats tuning by ear, since a
track skip is far longer than anyone can hold two versions of a sound in their
head. `StreamingEQ.update()` now re-applies the settings per chunk, comparing
first, so the steady-state cost is one tuple comparison ~23×/s. Three things
are load-bearing:

- **Instances persist.** Both processors carry filter and gain state; a new
  instance mid-track restarts the crossover and snaps the limiter's gain back
  to unity.
- **Enable/disable is a FLAG, not a `None` instance.** A bypassed limiter
  still holds its tail, so the stream keeps its 5ms latency rather than
  jumping forward on the toggle. A bypassed guard still runs the crossover and
  sums it — and LR4's halves sum magnitude-flat but the sum is an **allpass**,
  so that branch is deliberately not `return x`; a test pins the difference.
- **Crossover frequency and look-ahead are NOT settable.** Both own carried
  state, and both are measured values rather than taste ones.

**A change is heard `LEAD_S` (4s) later, and that is not a fault.** The feed is
paced that far ahead of realtime, so processing happens when the audio is
generated and the listener hears it a lead-time afterwards. Anyone A/B-ing must
wait ~5s before judging; quick toggling reads as "nothing happened" because the
old audio is still in the device buffer. Moving the chain onto the device is
the only real fix and is filed as #243 — the same argument that forced ducking
device-side.

**`bassGuardDb` barely moves the output, and the default hardly matters.**
Measured 2026-08-19 on a 50Hz + 1kHz mix at ordinary level: across the whole
range the depth changes 50Hz by ~9dB and the OVERALL level by **0.14dB**, with
1kHz unchanged. What is audible is the guard being **on at all** — −17.7dB at
50Hz and −5.0dB overall, and on loud material where the limiter is working the
midrange comes up **+2.6dB**. So tune with `bassGuardEnabled`, not with the
depth; an A/B between −20 and −40 is below audibility and will read as a broken
feature.

**The guard and the limiter cancel each other's most obvious cue, so "I hear
no difference" is not evidence.** The guard's effect on OVERALL LEVEL depends
entirely on whether the limiter is working. Measured 2026-08-20, guard on
versus off on the same bass-heavy signal:

| EQ            | overall | sub (30–80Hz) | 1kHz  |
|---------------|---------|---------------|-------|
| flat          | 7.68dB  | 24.4dB        | 0.0dB |
| +12 all bands | 0.17dB  | 20.4dB        | 4.2dB |

Under a heavy boost the limiter gives back exactly what the guard takes, so
the level difference goes to nothing and the whole change moves into the
midrange. An A/B run at that operating point tests almost nothing, and reads
as a dead control on a chain that is working perfectly — which is how an
entire morning went on 2026-08-20, after three earlier listening tests had
already failed for three unrelated real bugs. **A/B the guard at FLAT EQ**, or
not at all.

That is also why `em_eq.describe_chain` / `describe_activity` exist and are
logged per stream. Settings answer "did the config reach the audio"; max
reduction answers "did the law engage". `n/a` means bypassed and `0.00dB`
means running but idle — identical from a listening seat, opposite
investigations. `em_limiter` keeps `release_ms` purely so it can be reported;
the gain law uses the derived slew.

**Guard before limiter.** Limiting first spends gain reduction on bass that is
about to be discarded, pulling the midrange down for no reason. Measured on a
50Hz + 1kHz mix: with the guard on, the 50Hz component drops 17.2dB and the
1kHz component gets **0.5dB louder**, because the limiter no longer has to
hold the whole signal down to contain bass peaks nobody was going to hear.

**The EQ used to hard-clip** (#231). `np.clip` ended both paths with no
headroom management, and the dashboard offers ±12dB faders plus a presence
boost that stacks on top — measured at 4.74% of samples clipped at −1dBFS with
a modest bass boost, 17.95% at the top of the sliders. A flat EQ short-circuits
and returns the input untouched, which is why it went unnoticed: it hit exactly
the people who reached for the controls to improve their sound. A gain trim is
the obvious fix and the wrong one, since it costs the full boost in level.

**The bass guard's parameters are measured, not chosen.** Stock's
`/system/vendor/etc/audio-algorithms/MBCL.cfg` on this speaker: crossover
115Hz, ratio 20:1, threshold −50dB, floor −40dB, release 200ms. Stock's bands
2–4 are deliberately not implemented — one gentle 2:1 law at −10dB repeated
three times, which is broadband compression for loudness rather than
protection. Our default depth is **−30dB rather than stock's −40dB**, because
stock's sits in front of stock's own EQ curve, which we have neither nor have
measured; copying the depth without the curve it was tuned against is not the
same setting. It shipped at −20 and moved to −30 after the first listening
test that heard the chain working (2026-08-20), on the grounds that mid-range
leaves room to go either way — the depth is very nearly a free choice, since
the whole range moves the overall level 0.14dB.

**The crossover is Linkwitz-Riley, and the first attempt was not.** LR4's
lowpass and highpass sum flat (measured: 0.0000dB deviation across 4096
points), so the guard cannot colour a stream it is not compressing. The first
implementation split subtractively (`rest = x - lowpass`), which reconstructs
exactly by construction and looks obviously right — and does not work: at 60Hz
the lowpass passes 0.998 of the signal and the residual is **1.279**, larger
than the input, because subtracting a phase-shifted copy is not removing a
band. 20dB of in-band reduction produced 0.4dB at the output. A test pins that
measurement so nobody simplifies back to it.

**Both processors carry state and must be one instance per stream.** Sharing
one would let a voice response duck the music underneath it. Both are also
bit-identical between the chunked and one-shot paths, since TTS arrives as one
buffer and music as many; `em_limiter`'s chunk seed must be `_gain_db + _slew`,
not `_gain_db`, or the two drift apart.

**Both are gain-staged the same way and use the same maths**: instant attack,
slew-limited release, written as a running extremum in a sheared coordinate
system so it is exact and vectorised rather than a sequential recursion. The
limiter's threshold is taken against 32767, not 32768 — int16 is asymmetric, so
a 0dBFS threshold against 32768 produces a sample that WRAPS to full-scale
negative on the cast.

What stock does that we do not, and why (#229): its six `EQ_*.cfg` files are
**one filter at six gains** (`EQ_100` is `EQ_50` × 5.334838 exactly, +14.54dB),
so tone is constant across volume and there is no volume-banded EQ to copy. A
measured driver response is the remaining unknown, and the item that needs
hardware.

## Ducking: music and voice are separate planes on the device

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

## Timers, and owners are COUNTED not flagged

Voice-assistant timers (#167, @bluescreen10) make the alarm ring a **fourth
owner of the speaker**, alongside voice, music and announcements. `em_timers.py`
holds the matchers and constants; `start_timer_alarm` / `stop_timer_alarm` /
`_ring_timer_alarm` in `em_controller.py` drive it. Bursts are gated on
`device.speaker_busy`, dismissal sends `speaker_flush` (or the ring plays out of
~5.5s of device buffer after it has been stopped), and an unanswered ring stops
at `MAX_RING_S` = 120s.

**The ring asks before writing the plane; the announcement does not, and that
is #373.** `_ring_timer_alarm` gates every burst on `speaker_busy` because two
writers would interleave frames on `0x02` — but `_standalone_play` performs no
such check and streams straight into a chime already in flight. Measured
2026-08-28: an announcement landing between bursts plays, one landing during a
burst is **inaudible**. Both paths also share a single `device.playback_done`
Event, so one device report satisfies two waiters (observed as two `Playback
complete` lines in the same millisecond, and an announcement whose wait ended
after a chime's duration rather than its own). **The exclusion being
one-directional is the bug** — do not "fix" it by blocking announcements while
ringing, because HA blocks on the announce call holding `_is_announcing` and a
120s `MAX_RING_S` would fail every other announcement to that satellite. See
`docs/audio-states.md` §6 Q4 for the options, including the longer-term move of
the alarm onto the music plane (which needs `audio_mix` gating, or the alarm is
silent on firmware that cannot mix).

**A cancelled playback must release `speaking` (#366).** `_run_post_turn_playback`
clears it in the `finally`, beside `speaker_busy`, and shielded — it used to sit
after the try, so `stop_timer_alarm`'s cancel skipped it and the flag stayed set
for the life of the process. That is not a cosmetic tile: the wake listener
skips every frame while `speaking` is set and no alarm is ringing, so the device
went **permanently deaf** after a mid-chime dismissal. Roughly three dismissals
in four hit it, the chime being 1.68s of every 2.3s.

**HA hands ringing to the satellite and expects the satellite to own dismissal**
— the same shape its own Voice PE hardware has. So the dismissal is recognised
here, from the transcript HA already sends, rather than waiting for a CANCELLED
that structurally will not come. The registry's CANCELLED path stays for the
cases HA *does* answer.

**Two dismissal matchers, and they must not be collapsed into one.**
`is_dismissal` is deliberately generous, because a missed dismissal leaves the
alarm ringing and HA answering "there are no timers", which is far worse than
an extra stop. `is_dismissal_only` is strict, because it suppresses HA's reply
— and a false positive there is not a spare stop, it is a **lost answer**.
"Turn off the kitchen light" over a ringing alarm is a dismissal by the
generous rule (correctly — the alarm should stop) and also a real command HA
answers; suppressing that left the light off and the user unable to tell
whether anything had happened. Note it is the `off` variant that breaks, not
`on`. Phrases are stripped **longest-first** so `turn off` is consumed before
the bare `off` strands `turn` as an unexplained word.

**Overlapping owners are COUNTED, and this is the bug class the two fixes
share.** `ducked` was one boolean per session, so a barge-in turn or an
announcement landing mid-turn both set it and whichever finished FIRST sent
`duck: false` while the other was still speaking (#261). `owned_by_turn`,
`pending` and `resume_after` had exactly the same shape (#314): an announcement
ending mid-turn released the turn's ownership, and a `play_media` arriving then
went straight to the wire and put music under the response — the precise thing
`interrupt()` exists to prevent.

- `duck_depth` and `owner_depth` are **separate counters on purpose.**
  `duck_depth` only increments on the mixing path (`audio_mix_capable`), so a
  device that pauses instead of ducking has overlapping owners and no duck
  depth at all. Reusing one as the other is correct everywhere except on
  exactly those devices, which is the worst kind of wrong.
- The wire command goes out only on the **0→1 and 1→0 transitions**, never on
  the intermediate ones.
- `s.lead_s` follows `duck_depth`, not the first release — holding `TURN_LEAD_S`
  while another owner still has the duck.
- Only the FIRST claim clears `pending`. A nested `interrupt()` must not wipe a
  playback command the user genuinely issued during the turn.
- **The hazard refcounting introduces is a leaked owner**, which turns a
  self-healing transient into permanently quiet music. Both call sites are
  balanced across `try`/`finally` (the voice turn and the announcement path);
  keep them that way.

**The alarm is a fourth owner and the state map is still owed** — `speaker_busy`
handles it as far as the ring goes, but voice/music/announcement/alarm has no
single written ladder. `docs/audio-states.md` §2 is the nearest thing.

## Key Python modules

| File | Role |
|------|------|
| `em_controller.py` | WebSocket server, `Device` registry, voice pipeline, mDNS |
| `em_api.py` | aiohttp HTTP API + dashboard SPA, OTA, shell proxy |
| `em_db.py` | SQLite persistence (devices, config, logs, users) |
| `em_auth.py` | Session auth with bcrypt |
| `em_eq.py` | Parametric EQ applied to TTS and music before playback; also hosts the chain, calling the guard and limiter in order |
| `em_mbc.py` | Dynamic bass guard — drops low frequencies the driver cannot deliver. Pure, unit-tested; parameters measured off stock |
| `em_limiter.py` | Look-ahead peak limiter — stops the EQ clipping what it boosts. Pure, unit-tested |
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
| `em_runbarrier.py` | Serialising ESPHome pipeline runs across a barge-in, as a pure state machine. The protocol carries **no run identifier**, so the satellite is what keeps two runs from overlapping — see the barge-in rules under the voice backend. Split out for `em_linkauth`'s reason: the suite cannot import `em_esphome` |
| `em_announce.py` | Running an HA announcement to completion. Owns the two rules that pull against each other — never reply early, always reply — because `VoiceAssistantAnnounceFinished` is HA's completion signal and HA **blocks** on it |
| `em_linkauth.py` | The device-link auth decision as a pure function. Split out of `em_controller._link_auth_ok` so it is testable: the suite does not import em_controller, so this was security logic with no coverage until it orphaned a device |
| `em_timers.py` | Voice-assistant timers (#167) — the alarm ring, and the two dismissal matchers that must NOT be one. `is_dismissal` is generous because a missed dismissal leaves the alarm going and HA answering "there are no timers"; `is_dismissal_only` is strict because it suppresses HA's reply, and a false positive there is not a spare stop, it is a lost answer ("turn off the kitchen light" over a ringing alarm). Phrases are stripped longest-first so `turn off` is consumed before the bare `off` strands `turn` |
| `em_ble_proxy.py` | BLE proxy ESPHome servers — a second, separate ESPHome device per Echo (own port from the shared counter, own mDNS, MAC = serial-derived with the locally-administered bit flipped). Forwards `ble_adverts` control messages from the device's passive scanner (`device/internal/bluetooth`, raw HCI over `/dev/stpbt`; enabling durably disables Android's BT stack) to HA as raw advertisements. Lifecycle = idempotent `reconcile()` driven by `bleProxyEnabled` |
| `esphome/` | ESPHome native API protocol layer (framing, handshake, vendored protobufs) |

## Fleet vs device scoping (schema v8)

The wire contract for the config push — which keys exist and which the device
ignores — is in the repo-root `CLAUDE.md`. This is how a device's effective
values are resolved before that push.

Scoping is **per section**, not one boolean. `em_config_sections.py` is the single source of truth mapping each config key to one of six sections (playback, wakeword, microphones, ring, advanced, bluetooth); `devices.config_sections` stores the set a device overrides, and `get_effective_device_config` = fleet overlaid with the device's values for those sections only. `use_global_config` survives as a derived compat view (no sections == fleet) and must not be treated as authoritative.

Three invariants, each guarded by `tests/test_config_sections.py`:
- **The partition must stay total** — a new key in `DEFAULT_DEVICE_CONFIG` that belongs to no section can never be overridden and never renders. Add the key to a section in the same change.
- **`dashboard.jsx`'s `CONFIG_SECTIONS` mirror must match Python** — it is parsed as JSON out of the file, so keep it comment-free and double-quoted. Drift puts a control under a toggle that does not govern it.
- **`STATE_KEYS` (`startupVolume`) are never section-scoped** — persisted device state, always taken from the device, never fleet-inherited.

Reverting a section **discards** its stored values (`set_device_config_sections` prunes), so no shadow values resurrect on a later re-override. Both config write paths push the **effective** config via the shared `_apply_live_config`, never the request body — with per-section scoping a body is partial by design, and the fleet endpoint now pushes every connected device rather than only fully-inheriting ones.

**A fleet edit to a section a device overrides silently does nothing to that
device, and the dashboard reports success.** It is the merge working as
designed — `em_config_sections.merge` layers the device's stored values over
the fleet's for its overridden sections — but from the front it is a control
that saves, says "pushed", and changes nothing. It cost an hour of a listening
test on 2026-08-19: fleet `eqBands` was set to −12dB across all eight bands, an
18dB drop that is impossible to miss, and nothing happened, because the device
being listened to overrode `playback` and kept its own curve. Note the failure
is **per key, not per push** — the same save can apply half its values and
discard the other half, depending on which sections each key belongs to.
Absent keys DO fall through to fleet (`if key in device_cfg`), which is why a
device overriding `playback` before the output-chain keys existed still
receives them from the fleet.

The rule this project already holds elsewhere applies: a control that cannot
act must say so rather than appear to work.

**Every controller-consumed key needs mirroring in BOTH places, and a test
enforces it.** Config reaches the running controller as attributes on `Device`,
set in `em_controller.handle_control` when a device registers AND in
`em_api._apply_live_config` when someone saves. A key in the first but not the
second reads as working: the database is written, the device is sent a value it
discards, and the setting takes effect at the next reconnect — which is exactly
what someone does before investigating further. That happened to all five
output-chain keys, which have no device-side consumer at all, so the mirror was
the only thing that could have carried them.
`tests/test_config_mirrors.py` diffs the two sites; `startupVolume` is the one
deliberate exemption, because it is device state and a later push must not
stomp a volume changed by hand.

## Persistent activity stats

Every voice turn is persisted to SQLite at completion (`turns` table, `db.insert_turn` from `em_esphome`): trigger, wake model/score/threshold, room noise floor at detection, outcome, STT text, stage latencies, and playback underruns.

**Delivery instrumentation (schema v7, firmware v2.9.6+).** Underruns are rare and binary; these measure the *margin* on every stream so degradation is visible before it's audible. Device-reported in `playback_stats`: `min_depth` (fewest periods left in the device buffer mid-stream — the headline number), `prime_wait_ms`, `recv_span_ms` (first→last frame arrival; longer than the audio duration means delivery was slower than realtime), `max_gap_ms`, `bytes_recv`. Controller-measured: `send_ms`, `delivery_ms` (first frame sent → device's `playback_stats` arrival), `eq_ms`. **`send_ms` is a socket-write time and completes near-instantly however slow the link is — never read it as delivery; that mistake cost a whole investigation on 2026-07-20.** `device_metrics` gained link context (`link_speed_last/min`, `wifi_freq_last`, `wifi_bssid_last`, tx/rx byte and error sums) — band and BSSID matter because one SSID spanning 2.4/5GHz lets a device silently re-associate to a much slower radio. `event_loop_lag_monitor` tracks controller-side stalls (peak on `/api/system/status` as `loop_lag_peak_ms`); anything blocking the loop also delays speaker frames. **That peak reads 0 under the add-on and always has — #306.** `em_start.py` execs `em_controller.py`, so the running module is `__main__`, while `/api/system/status` and the support bundle both `import em_controller` and get a SECOND module object whose global is still the initial 0.0. The logged warnings are correct; the reported peak is not, so read the log line and not the field until that is fixed. It resolves correctly under docker-compose, which is why it survived — the deployment most users run is the one where it lies. The underrun count arrives asynchronously — the device reports `playback_stats` (periods + underruns) once per completed speaker stream, and the controller attaches it to `device.last_turn_id` (consumed on use so an announcement's report can't overwrite a turn's stats; NULL underruns = never reported, e.g. pre-v2.9 firmware). Two hourly rollup tables ride alongside: `wake_counters` (near-miss counts/max score, flushed through the existing 2s-rate-limited near-miss path; plus non-turn underruns) and `device_metrics` (CPU/RAM/storage/RSSI sums+extremes upserted per ~30s device stats report — averages computed at read). `Device.turn_history` is hydrated from `turns` on connect, so the dashboard Activity tab survives restarts. Read APIs: `/api/devices/{id}/turns` (raw, `limit`/`since`) and `/api/devices/{id}/activity?days=N` (per-day aggregates, per-wake-model rollups, counters, metrics — plot-ready). Keep instrumentation at this cost class: one insert per turn, one upsert per 30s/2s — nothing per audio frame. The v7 device counters honour this: per-period work is one `len(chan)` compare plus one `time.Now()` on a single-writer path (no locks, no allocation, no logging), all of it emitted on the *existing* `playback_stats` message. `wpa_cli` is the one exception that costs a process spawn, so `linkInfo()` caches it for 2 minutes rather than running per stats tick.

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

**Updates are SERIALISED across the whole controller, and the queue is
bounded.** Three concurrent OTAs stalled the event loop for 11.1 seconds
(measured 2026-09-02 updating three devices to v2.14.0, in the `[loop] event
loop stalled` warnings — the reliable source, since the reported peak reads 0
under the add-on, #306). That loop sends speaker periods and LED frames, so a
device answering someone pays for a device being updated.
`_updates_in_progress` could never have prevented it: it stops ONE device
being updated twice and says nothing about two at once. So `_ota_lock` is
global and both entry points go through it — the fleet deploy and a
hand-clicked single update collide identically, and only the first was ever
going to be noticed.

- **The binary is fetched inside the lock**, so a queued device holds nothing
  but its place in line, and the lock is released in a `finally` — an update
  that raises would otherwise hold it for the life of the process and no
  device could be updated again without a restart.
- **A failure does not stop the queue.** Mark it, carry on, report at the end:
  one device that will not come back must not strand a fleet update behind it.
- **`OTA_MAX_HOLD_S` (300s) caps the hold**, because serialising turns a
  device-local stall into a fleet-wide one. Every `recv` in
  `_stream_file_to_device` is `wait_for`-bounded but `await ws.send(line)` in
  the base64 loop is not, and a device that stops reading applies backpressure
  and can hang there. `_run_update` is a thin wrapper around
  `_run_update_locked` so the whole of the update sits under one timeout.
- **Queued is reported separately from in-progress** (`update_queued`), and
  rendered as "queued": a device that has been started and not yet touched is
  not having a transfer, and claiming otherwise is the same failure as any
  control that appears to work.

**Every payload reconciles on OTA or on a click, and nothing reconciles on
CONNECT — which is the wrong trigger and is why devices drift.**
`_sync_start_script` and `_sync_debloat` run inside `_run_update_locked` and
from the Maintenance button; `reconcile_oww_assets` does run on connect but
returns early unless `owwOnDevice` is on, and then checks only the SELECTED
classifier. So a device can sit for weeks missing three of the four stock wake
words — measured on Office 2026-09-02, provisioned 17 Aug with `hey_jarvis`
alone — while every panel reports it healthy. A device arriving is exactly the
moment we know what it has. Wil's call, same day: reconcile all three payloads
on connect, debounced per device.

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

**All three payloads reconcile when the device CONNECTS** (`em_api.reconcile_on_connect`, called from the register handler). A device arriving is the one moment we know what it has, and until 2026-09-02 nothing used it: the wake word assets reconciled here but returned early unless the device scored locally and then checked only the selected classifier, while `_sync_start_script` and `_sync_debloat` ran **only** inside an OTA or from the Maintenance button. So a device already on the latest firmware never received a payload change at all — Office sat without three of the four stock classifiers for a fortnight with every panel calling it healthy. Four rules:

- **Sequential, never gathered.** All three talk to one device over one shell plane; concurrency contends for a single session and none of them is on the critical path of anything.
- **One failure must not skip the other two.** Unrelated payloads — a device with a stale debloat list should still get its wake word models — so each step is caught individually, not the loop.
- **Debounced per device** (`RECONCILE_DEBOUNCE_S`, 15 min), because reconnects are routine on this fleet and the payloads are not; they change when someone deploys or edits a config, which is minutes to days apart. The stamp is claimed **before** the work, so a device reconnecting mid-run cannot start a second one against the same shell plane. `_delete_device` calls `forget_reconcile` — a re-added device is the one whose payloads are least likely to be right.
- **A silent device is not a missing file.** `_shell_run` swallows every exception and returns `""`, so an absent md5 and a device that never answered were the same string — and the syncs read empty as out-of-date. That was harmless while they only ran mid-OTA against a shell already proven; seconds after connect the shell plane is very likely **not up yet**, so it meant a pointless push and a user-visible "out of date" event that was untrue. Both syncs now append `_SHELL_OK` to the probe and return untouched without it. Same shape as `reconcile_oww_assets`'s "failure to LOOK is not evidence of absence".

Note the mode gate is deliberately kept: with `owwOnDevice=off` the device scores nothing and the 12.3MB runtime is irrelevant, so the assets reconcile still returns early there. The other two payloads are md5 compares and run regardless.

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

**Every route that ends a turn's speech must call `_enter_thinking`, and there
are two (#370).** HA's `STT_VAD_END` event and the device VAD sentinel in
`_stream_mic_audio` both mean "the user stopped talking"; only the first was
wired to `on_thinking`, so a turn ending on the sentinel held the listening ring
through the whole STT/intent/TTS window — ~11s measured. It presented as "the
button is slower than the wake word" and the trigger is a red herring: there is
exactly ONE deliberate branch on it in the turn path (`preroll_discard`), and
which endpoint route wins is a race. 33/33 wake turns ended on HA's VAD, 11/11
button turns on the sentinel, but nothing holds that on a slower link.
`_enter_thinking` is idempotent because on a slow turn both routes fire.
`tests/test_thinking_transition.py` pins that `_on_thinking` has exactly **one
call site**, so a third endpoint route gets the ring right for free — and that
the no-speech timeout deliberately does NOT enter it, since nothing was said.

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
