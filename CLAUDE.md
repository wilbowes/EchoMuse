# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

EchoMuse repurposes Amazon Echo Dot Gen 2 (FireOS 5 / Android 5.1, codename "biscuit") as an open-source voice assistant satellite. Two components:

- **`device/`** — Go binary that runs directly on the rooted Echo Dot
- **`controller/`** — Python asyncio WebSocket server that manages devices, runs wake word detection, and proxies to a voice pipeline
- **`oww_forge/`** — standalone Docker batch trainer for custom openWakeWord models (synthetic TTS positives → augmentation → classifier head → `.onnx`). Not part of the controller; see `oww_forge/README.md`. Upstream pins in its Dockerfile are load-bearing (piper-sample-generator v2.0.0 flat layout; openWakeWord SHA with a `--convert_to_tflite` argparse patch). Models install via the dashboard (Config → Wake word → "+ Custom model" → `/api/oww_models/upload`) into `oww_models/` beside the SQLite DB; `owwModel` stores the file path for custom models. openwakeword keys predictions by filename *stem*, never the path — always score via `em_oww_models.prediction_key`

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
python -m pytest tests/        # needs: pytest numpy scipy — not the full requirements.txt
```

Controller tests cover the pure-logic modules only (`em_eq`, `em_scenes`, `em_oww_models`, `version`) — keep it that way unless you're prepared to pull openwakeword/aiohttp into the test environment. Both suites (plus `go vet`) run in CI on every push/PR (`.github/workflows/ci.yml`).

**Release:** pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds the binary in the compiler image and attaches it to a GitHub release. **Tag with `git tag -a`** — the annotation message becomes the release body (`body_path` from `git tag -l --format='%(contents)'`), which is what the dashboard shows next to an available update. Write it for the person deciding whether to push firmware to a device they depend on: what changed, what to expect, anything required of them. GitHub's generated commit list is still appended below it. A lightweight tag yields an empty body and falls back to that list, which is a worse experience, not a broken one.

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
- **Controller**: `controller-v*` tags (e.g. `controller-v2.8.0`) → `controller-release.yml` → Docker image pushed to `ghcr.io/wilbowes/echomuse-controller` (`X.Y.Z` + `latest`, CPU-only, amd64). **No GitHub Release is created** — the OTA system's release polling (`em_api._fetch_latest_release`) filters for `v*` tags with a `server` asset, but controller releases stay out of the releases list entirely by design. **Tag controller releases with `git tag -a` too**: with no Release behind them, the annotation is the *only* copy of the notes, and it is what the dashboard's controller-update notice displays (`em_api._fetch_controller_release` reads it via `git/matching-refs` + the tag object). A lightweight controller tag ships an image nobody can read a changelog for. Pick the newest tag by **parsed version, never list order** — the refs API sorts lexically and returns `controller-v2.9.0` *after* `controller-v2.10.0`.

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

### Voice backend

The controller impersonates ESPHome voice satellites: one asyncio TCP listener per device on ports 16001+ (persisted in the device registry, never reused). Home Assistant's built-in ESPHome integration dials in and drives voice turns via Assist. Implemented in `em_esphome.py` on top of the protocol layer in `controller/esphome/` (`frame_protocol.py`, `satellite_server.py`, vendored aioesphomeapi protobufs in `esphome/vendor/`). Servers are created at startup for every approved device **and on demand** when a device approved after boot first connects (`_register_device_server` — idempotent on purpose: the startup loop and `device_connected()` race, first creation wins). HA naming: friendly name is `<label> Voice Assistant` (BT proxy: `<label> BT Proxy`); `project_name` carries `ESPHOME_DEVICE_MODEL` after the dot because HA displays that segment as the device Model, overriding DeviceInfo's `model` field. (A legacy `claracore` WebSocket backend was removed 2026-07-12 — ESPHome/HA is the only voice path.)

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
- **AEC** (`internal/aec/`) — speexdsp echo canceller (vendored C, SpeexDSP-1.2.1), whole mic path including the wake stream; far-end reference tapped at the speaker ALSA write (every period incl. silence), delayed by `aecDelayMs` — **keep 0**: the mic side's 160ms batch reads absorb the speaker's output latency, and higher values make the echo non-causal (zero cancellation). The mic ALSA ring is only 160ms deep, so >160ms capture stalls silently lose whole batches (~every 20–30s in steady state, load-correlated); an occupancy governor trims the resulting reference backlog **without resetting the filter** — the trim restores the alignment the filter converged against, and the reset that used to live there thrashed convergence to ≤5dB (the v2.7.8 fix). `[aec] att=`/`far:` telemetry logs ~1/s during playback; `[mic] clock/stall` lines track capture loss. Default off (`aecEnabled`); ~14dB per response, held across turns
- **Barge-in** (controller-side `_barge_watcher`) — wake word spoken during TTS cancels playback (device does a stateful `speaker_flush`: drains buffer + discards until stream EOS, since the rest of the stream is typically still in TCP buffers; controller-side, both `stream_speaker` and the post-playback drain sleep race `cancel_event`). `bargeInThreshold` is used as-is and sits *below* `owwThreshold` by design (0.05–0.10): echo at the mic is ~25dB louder than the person, so speech-over-TTS scores are depressed (~0.3–0.5 observed), while converged self-echo scores 0.002–0.003
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
| `internal/bindings/als/` | Ambient light (ams **TSL2540** on i2c). Android does not expose it AT ALL — `dumpsys sensorservice` reports an empty list, nothing under `/sys/class/sensors` or `/sys/bus/iio`, no input device; it is visible only on the raw i2c bus, the same shape as the mute LED being on a different GPIO than the vendor HAL believed. Resolved **by name, not address** (`0-0039` is an enumeration accident; a second ALS, `tsl2584tsv`, is enumerated but undriven). `Lux()` returns **nil, never 0** — a covered sensor reads a genuine 0. `Watch` reports a step change immediately (25% relative, 10-lux floor, measured noise ±1.5%); the steady value rides the ~30s stats tick |
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
| `em_recordings.py` | Utterance capture storage — WAVs in `recordings/` beside the DB, per-device file-count retention, ownership-checked path resolution |
| `em_linkauth.py` | The device-link auth decision as a pure function. Split out of `em_controller._link_auth_ok` so it is testable: the suite does not import em_controller, so this was security logic with no coverage until it orphaned a device |
| `em_ble_proxy.py` | BLE proxy ESPHome servers — a second, separate ESPHome device per Echo (own port from the shared counter, own mDNS, MAC = serial-derived with the locally-administered bit flipped). Forwards `ble_adverts` control messages from the device's passive scanner (`device/internal/bluetooth`, raw HCI over `/dev/stpbt`; enabling durably disables Android's BT stack) to HA as raw advertisements. Lifecycle = idempotent `reconcile()` driven by `bleProxyEnabled` |
| `esphome/` | ESPHome native API protocol layer (framing, handshake, vendored protobufs) |

## On-device wake word (shadow mode)

The Echo can run the wake model itself. `owwOnDevice` = `off` (default) or
`shadow`; a third mode letting the device *trigger* turns is deliberately not
implemented, and an unknown value normalises to `off` rather than being guessed
at — the two plausible guesses are "score silently" and "start triggering", and
one of those is a live behaviour change on a device that cannot honour it.

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
- `DEVICE_DIR`, the shared model names and the classifier stem rule are pinned
  against the firmware constants **by test**. Drift installs assets the device
  never looks for, and the only symptom is shadow mode silently never starting.

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

**Control-plane RTT (schema v9/v10).** The RF layer is OPAQUE on this hardware and its counters are worthless: the MTK driver leaves retry/discard/missed-beacon at zero in `/proc/net/wireless` whatever the link is doing, reports `NOISE=9999`, and there is no `iw` binary — so `tx_errors`/`tx_dropped`/`rx_crc` are STRUCTURALLY zero and `get_device_metrics` deliberately does not surface them (a zero there reads as "healthy link" and is not). RTT is the latency signal that works: the controller stamps each control-plane `ping` with a sequence id (every `PING_INTERVAL_SEC`=5s), the device echoes it, and RTT is computed against one monotonic clock — the device never stamps its own, because Echos boot with bogus clocks pre-NTP. Unsolicited keepalive pongs carry no id and are ignored rather than paired with whatever ping is outstanding. Samples aggregate in memory (`Device.record_rtt`/`drain_rtt`) and flush on the existing ~30s stats report, so the DB cost is unchanged; note this means **adding an RTT field needs `drain_rtt` updated as well as `record_device_stats`** — the relay guard in `tests/test_db_instrumentation.py` covers both sources. Excursions (≥`RTT_EXCURSION_MS`=200) are split by whether the device was busy at SEND time, and `rtt_samples_idle` is the denominator that makes the split meaningful: without it "every excursion was idle" is vacuous, since almost every sample is idle. Read API exposes per-state RATES, never raw counts. **Measured 2026-07-25: 15-35% of probes exceed 200ms on ALL THREE devices including one at −26dBm on its own AP, with idle and busy rates indistinguishable (Lounge 33.3% vs 36.5%) — which rules out signal strength, WiFi power-save and load contention alike. Still unexplained; next step is ICMP-vs-app-RTT to separate network from above-network.**

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

Volume is **state, not a setting** — it rides the config channel but has no dashboard control (the slider was removed 2026-07-25: `SeedVolume` ignores later pushes, so moving it did nothing until the device restarted and any real volume change overwrote it). It is listed in `em_config_sections.STATE_KEYS`, exempt from section scoping, and shown read-only on the Status tab.

Volume persists through reboots **controller-side**: every device `volume_state` report is stored into the device's `startupVolume` config, and the device restores it via `Server.SeedVolume` on the **first config push per run only** (later pushes must not stomp live changes). Until seeded (or a local volume change makes the device authoritative), the device suppresses its connect-time `volume_state` report — reporting the boot-default level is what used to clobber the stored value on reboot. Mute is the opposite: **device-sovereign**, persisted locally in `/data/local/etc/echomuse/state.json` (survives OTA slot flips; written on toggle, restored at boot pre-connect — ADC mute immediately, red ring/button LED after LED init).

## LED priority system

Turn-state ring colours (listening ring, thinking spinner) come from **LED scenes** (`em_scenes.py`), configurable per device (`ledScene` + custom colours). Firmware with the `led_anim` capability (v2.9+) **animates locally**: the controller sends one `led_anim` message per state change ({pattern: solid|spin|rotate|pulse|meter|off, colors, periodMs, ttlSec}) and the device renders frames on its own ticker (`internal/server/animator.go`) — controller/WiFi jitter can't judder the ring. `meter` throbs with the live speaker RMS (tapped at the ALSA write, so it tracks audible audio, not the ~5.5s-ahead send) — measured on the **voice plane only, before the music mix**, unlike the AEC far-end tap which deliberately sees the mixed output; a meter fed the mix throbs to the music bed before the response has started; its response curve is config-tunable (`meter*` keys → `AnimSpec` pointer fields → `resolveMeter`, which clamps independently of the dashboard ranges) because it is a taste parameter that needs iterating in a real room, not a firmware OTA per pass. `ttlSec` is bounded per phase — 30s listening, 135s spinner (**coupled to `_fetch_tts_audio`'s 60s timeout ×2 attempts, since the spinner spans HA think time AND the fetch — move one and move the other**), and computed per response for `meter` via `em_scenes.meter_ttl` so a long TTS cannot self-clear mid-answer. Loss-resilience: newer spec or raw `leds` frame atomically replaces the animation (generation counter), and `ttlSec` is a dead-man that self-clears the ring if the controller dies mid-turn. Legacy firmware falls back to controller-streamed frames. Controller `leds` messages carry an explicit `listening: true` flag on listening-ring frames — the device's direction overlay keys off it (pre-scene firmware inferred "listening" from an all-green ring, which breaks for any other scene; the heuristic remains as fallback for old controllers). The direction overlay brightens the base ring colour instead of painting green. Mute ring (red) and volume arc (cyan) are device-local and scene-independent by design.

Turn *outcomes* are distinguished by rhythm, not colour (red/orange/cyan are taken by mute/link/volume): `no_speech` gets one slow throb, `no_tts`/`tts_error`/`timeout` fast blinks, everything else ends silently. Both ride the existing `pulse` pattern with a 1s TTL so they retire on the device's own ticker — no follow-up message to lose. Driven by `device.last_turn_outcome` (set in `em_esphome._persist_turn`, consumed once by `_leds_turn_end`).

Playback ring clearing waits for the device's `playback_stats` (`device.playback_done`), NOT a wall-clock estimate. The old estimate subtracted socket-write time — which completes near-instantly however slow the wire is — so it cleared the ring up to 6.1s early on exactly the links that needed longest. `playback_stats` is emitted once the audio channel drains after EOS, i.e. the real end of audio; the timeout is only a backstop for the report never arriving.

`server.go` maintains a `ledMode` (direction arc vs. system). System-level LEDs (controller commands, mute ring, pulse animations) always win over the beamformer direction arc. Two paint suppressions in `SetLEDs`/`SetDirectionLEDs` (state is still recorded in `baseLEDs` so the ring can be restored):

- **Mute ring** (solid red) is device-sovereign — enforced since v2.7.8: controller LED writes are recorded but not painted while muted. Needed because muting now terminates an active turn (controller cancels + `speaker_flush` on `mute_state`), so the cancelled turn's LED cleanup arrives after the red ring is up.
- **Volume arc** owns the ring for its 2s display window against *animations* — they repaint ~every 100ms and would otherwise stomp the arc within one frame. It does **not** outrank a deliberate action-button press: a dot release calls `CancelVolumeDisplay()`, which drops the hold so the listening frame paints (it deliberately does not repaint — the controller's frame lands within an RTT, and clearing to black would put a dark gap between the two). The arc is protection from repaint churn, not from the user. On expiry the ring repaints the latest `baseLEDs` frame (`onDisplayExpire` → `paintBaseLEDs`), handing back mid-animation. The arc shows only for physical volume button presses (v2.9.5): remote sets and the boot-time volume seed apply silently (`volumeController.Set` showRing flag). The mute-button LED is sysfs gpio444, active-high — not the gpio445 in Amazon's `libled_hal.so`, whose constant is off by one and whose pad is muxed away (stock drives the pin via the `/dev/mtgpio` ioctl; see `mute_button.go`).

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
