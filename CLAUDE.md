# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

EchoMuse repurposes Amazon Echo Dot Gen 2 (FireOS 5 / Android 5.1, codename "biscuit") as an open-source voice assistant satellite. Two components:

- **`device/`** — Go binary that runs directly on the rooted Echo Dot
- **`controller/`** — Python asyncio WebSocket server that manages devices, runs wake word detection, and proxies to a voice pipeline
- **`oww_forge/`** — standalone Docker batch trainer for custom openWakeWord models (synthetic TTS positives → augmentation → classifier head → `.onnx`). Not part of the controller; see `oww_forge/README.md`. Upstream pins in its Dockerfile are load-bearing (piper-sample-generator v2.0.0 flat layout; openWakeWord SHA with a `--convert_to_tflite` argparse patch). The controller consumes the output as-is: `owwModel` accepts a file path to a custom `.onnx` in addition to stock model names

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

Tests only cover pure-Go logic in `pkg/led/` — hardware-dependent code is not testable on the host.

**Release:** pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds the binary in the compiler image and attaches it to a GitHub release.

## Versioning / releases

Device firmware and controller are versioned independently from the same repo:

- **Device**: plain `v*` tags (e.g. `v2.7.6`) → `release.yml` → GitHub Release with the `server` binary asset. The tag is embedded in the binary and compared against `firmware_ver` by OTA — don't change this scheme.
- **Controller**: `controller-v*` tags (e.g. `controller-v2.8.0`) → `controller-release.yml` → Docker image pushed to `ghcr.io/wilbowes/echomuse-controller` (`X.Y.Z` + `latest`, CPU-only, amd64). **No GitHub Release is created** — the OTA system's release polling (`em_api._fetch_latest_release`) filters for `v*` tags with a `server` asset, but controller releases stay out of the releases list entirely by design.

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

The controller impersonates ESPHome voice satellites: one asyncio TCP listener per device on ports 16001+ (persisted in the device registry, never reused). Home Assistant's built-in ESPHome integration dials in and drives voice turns via Assist. Implemented in `em_esphome.py` on top of the protocol layer in `controller/esphome/` (`frame_protocol.py`, `satellite_server.py`, vendored aioesphomeapi protobufs in `esphome/vendor/`). (A legacy `claracore` WebSocket backend was removed 2026-07-12 — ESPHome/HA is the only voice path.)

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

Controller enforcement (`_link_auth_ok`): presented-but-wrong token always rejects; stored-token-but-none-presented is allowed (the credential push itself rides the plain shell plane — rejecting there would deadlock the rollout) until `REQUIRE_DEVICE_TLS=1` flips the posture to TLS+token mandatory. Flip it only when every device shows `wss (TLS)` in the dashboard (Status tab "Link" row; `linkTls` in `/api/devices`).

Credential delivery: the provisioning wizard installs credentials over adb pre-first-contact (`POST /api/provision/tls_credentials` mints the token + pending device row from the serial); already-fleet devices get the dashboard **Secure link** action (`POST /api/devices/{id}/secure_link` — shell-plane file push, then a connection bounce to redial over wss).

### Device audio pipeline

Each mic buffer passes through, in order:

```
raw 9ch S24_3LE → beamformer + fixed mic gain (micGainDb, applied to 24-bit samples) → mono S16_LE → [AEC] → [AGC] → [VAD gate] → /data WebSocket
```

Note the real buffer cadence: GoTinyAlsa's `GetAudioStream` reads the whole ALSA buffer per chunk (PeriodSize 512 × PeriodCount 5), so the mic pipeline runs on **160ms batches of 2560 samples**, not single 32ms periods. Anything assuming 512-sample buffers must handle multiples (this silently disabled AEC for four releases — see `aec.Process`).

The always-on wake stream (`mic_start` without `lock_mic`) is **ungated and AGC-free**: every 32ms period is sent continuously (batched into 80ms frames) so openwakeword scores an uninterrupted stream, and no adaptive gain state can drift with room noise. The VAD gate and AGC apply only to bounded `lock_mic` turn streams (button-triggered), which get a fresh `ResetAGC()` per stream.

- **Beamformer** (`internal/beamformer/`) — selects the perimeter mic with the highest onset energy ratio (fast/slow EWMA) at voice turn start, then locks for the duration. Its `extractChannel` also applies the fixed mic gain (`micGainDb`, default +24dB) against the full 24-bit sample before quantising to S16 — captured speech sits at ~−70dBFS, so gain must happen pre-truncation to recover real resolution. `vadThreshold` stays in pre-gain units (the device scales it by the gain internally)
- **AEC** (`internal/aec/`) — speexdsp echo canceller (vendored C, SpeexDSP-1.2.1), whole mic path including the wake stream; far-end reference tapped at the speaker ALSA write (every period incl. silence), delayed by `aecDelayMs` — **keep 0**: the mic side's 160ms batch reads absorb the speaker's output latency, and higher values make the echo non-causal (zero cancellation). The mic ALSA ring is only 160ms deep, so >160ms capture stalls silently lose whole batches (~every 20–30s in steady state, load-correlated); an occupancy governor trims the resulting reference backlog **without resetting the filter** — the trim restores the alignment the filter converged against, and the reset that used to live there thrashed convergence to ≤5dB (the v2.7.8 fix). `[aec] att=`/`far:` telemetry logs ~1/s during playback; `[mic] clock/stall` lines track capture loss. Default off (`aecEnabled`); ~14dB per response, held across turns
- **Barge-in** (controller-side `_barge_watcher`) — wake word spoken during TTS cancels playback (device does a stateful `speaker_flush`: drains buffer + discards until stream EOS, since the rest of the stream is typically still in TCP buffers; controller-side, both `stream_speaker` and the post-playback drain sleep race `cancel_event`). `bargeInThreshold` is used as-is and sits *below* `owwThreshold` by design (0.05–0.10): echo at the mic is ~25dB louder than the person, so speech-over-TTS scores are depressed (~0.3–0.5 observed), while converged self-echo scores 0.002–0.003
- **AGC** (`internal/processor/`) — lock_mic turns only; release is frozen during silence (RMS speech flag), preventing noise floor amplification. (Device-side RNNoise NS was removed 2026-07-12 — noise suppression is controller-side now: `em_ns.py`/DTLN on the ASR-bound stream, per-device `nsAsr` flag)
- **VAD** (lock_mic turns only) runs on pre-NS/AGC audio; opens gate after `VAD_SPEECH_MS` of speech, closes after `VAD_SILENCE_MS` of silence, then sends an end-of-speech sentinel

### Controller audio pipeline

1. **Wake word** — openwakeword (ONNX) runs in a thread executor per device on `mic_queue`
2. **Voice turn** — on wake or dot-button: drain stale frames → acquire `voice_lock` → stream mic to HA via the ESPHome satellite → receive TTS URL → fetch + ffmpeg-decode → EQ (`em_eq.py`) → resample to 48kHz mono → stream back as 0x02 frames
3. **Speaker** — the wire carries **mono** 48kHz (`resample_to_48k()`, numpy linear interpolation); the device duplicates L=R at the ALSA write (stereo ALSA config is an I2S/codec constraint, not a wire one). Device buffers ~5.5s (`audioChanDepth`) and holds playback until ~1s is queued or EOS arrives (`primePeriods`) — WiFi-stall protection for marginal links

### Key Go packages

| Package | Role |
|---------|------|
| `cmd/server.go` | Entry point: wires hardware, callbacks, and clients together |
| `internal/client/control.go` | WebSocket client to controller `/control` — registration, message dispatch |
| `internal/client/data.go` | WebSocket client to controller `/data` — mic streaming, speaker playback |
| `internal/server/` | Local state machine: mute, volume, LED mode priority |
| `internal/config/config.go` | Global runtime config; env var defaults, overridden by controller push |
| `internal/bindings/` | Hardware drivers: mic PCM, speaker PCM, LED I2C, button evdev |
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
| `em_scenes.py` | LED ring scenes — resolves `ledScene`/`ledListenColor`/`ledThinkColor` config into render-ready listening/spinner frames |
| `em_esphome.py` | ESPHome-mode satellite servers (`EchoMuseSatellite`, `DeviceESPhomeServer`) |
| `em_ble_proxy.py` | BLE proxy ESPHome servers — a second, separate ESPHome device per Echo (own port from the shared counter, own mDNS, MAC = serial-derived with the locally-administered bit flipped). Forwards `ble_adverts` control messages from the device's passive scanner (`device/internal/bluetooth`, raw HCI over `/dev/stpbt`; enabling durably disables Android's BT stack) to HA as raw advertisements. Lifecycle = idempotent `reconcile()` driven by `bleProxyEnabled` |
| `esphome/` | ESPHome native API protocol layer (framing, handshake, vendored protobufs) |

## Persistent activity stats

Every voice turn is persisted to SQLite at completion (`turns` table, `db.insert_turn` from `em_esphome`): trigger, wake model/score/threshold, room noise floor at detection, outcome, STT text, stage latencies, and playback underruns. The underrun count arrives asynchronously — the device reports `playback_stats` (periods + underruns) once per completed speaker stream, and the controller attaches it to `device.last_turn_id` (consumed on use so an announcement's report can't overwrite a turn's stats; NULL underruns = never reported, e.g. pre-v2.9 firmware). Two hourly rollup tables ride alongside: `wake_counters` (near-miss counts/max score, flushed through the existing 2s-rate-limited near-miss path; plus non-turn underruns) and `device_metrics` (CPU/RAM/storage/RSSI sums+extremes upserted per ~30s device stats report — averages computed at read). `Device.turn_history` is hydrated from `turns` on connect, so the dashboard Activity tab survives restarts. Read APIs: `/api/devices/{id}/turns` (raw, `limit`/`since`) and `/api/devices/{id}/activity?days=N` (per-day aggregates, per-wake-model rollups, counters, metrics — plot-ready). Keep instrumentation at this cost class: one insert per turn, one upsert per 30s/2s — nothing per audio frame.

## OTA update system

The device runs an A/B slot binary system:
- `/data/local/bin/server` is a symlink to either `server_a` or `server_b`
- `start_server.sh` counts fast exits (< 15s runtime); after 3 consecutive failures it flips the symlink to the other slot and exits, letting Android init restart with the fallback binary

OTA is triggered from the dashboard — the controller pushes the new binary via the `/shell` WebSocket.

Device-side payloads the controller distributes (`start_server.sh` via `/api/provision/start_script`; the debloat pair `debloat_packages.txt`/`echomuse-debloat.sh` via `/api/provision/debloat_packages`+`debloat_script`, applied by the wizard's Debloat step — pm hide list + Magisk service.d daemon stops) live canonically in `controller/device_payloads/` and are read from disk per request — never embed copies in `em_api.py` or `dashboard.jsx`. `device/scripts/start_server.sh` is a symlink into that directory. Every firmware OTA also syncs the device's `/data/local/bin/start_server.sh` against the canonical payload (`_sync_start_script` — md5 compare, heredoc push, rename into place; takes effect on next device reboot), so script drift heals fleet-wide without a separate update path.

## Device config push

`config.ConfigMessage` JSON fields (camelCase) are sent from controller to device on connect and on per-device config change. Non-zero fields are applied; zero/nil fields are ignored (partial update). Changes take effect immediately — no restart required.

Configurable parameters: `vadThreshold`, `vadSpeechMs`, `vadSilenceMs`, `owwThreshold`, `owwModel`, `adcDigitalGain`, `adcMicpga`, `micGainDb`, `startupVolume`, `beamAngle`, `beamformingEnabled`, `aecEnabled`, `aecDelayMs`, `aecTailMs`, `bargeInEnabled`, `bargeInThreshold`, `bleProxyEnabled`.

### Volume / mute persistence

Volume persists through reboots **controller-side**: every device `volume_state` report is stored into the device's `startupVolume` config, and the device restores it via `Server.SeedVolume` on the **first config push per run only** (later pushes must not stomp live changes). Until seeded (or a local volume change makes the device authoritative), the device suppresses its connect-time `volume_state` report — reporting the boot-default level is what used to clobber the stored value on reboot. Mute is the opposite: **device-sovereign**, persisted locally in `/data/local/etc/echomuse/state.json` (survives OTA slot flips; written on toggle, restored at boot pre-connect — ADC mute immediately, red ring/button LED after LED init).

## LED priority system

Turn-state ring colours (listening ring, thinking spinner) come from **LED scenes** (`em_scenes.py`), configurable per device (`ledScene` + custom colours). Firmware with the `led_anim` capability (v2.9+) **animates locally**: the controller sends one `led_anim` message per state change ({pattern: solid|spin|rotate|pulse|meter|off, colors, periodMs, ttlSec}) and the device renders frames on its own ticker (`internal/server/animator.go`) — controller/WiFi jitter can't judder the ring. `meter` throbs with the live speaker RMS (tapped at the ALSA write, so it tracks audible audio, not the ~5.5s-ahead send). Loss-resilience: newer spec or raw `leds` frame atomically replaces the animation (generation counter), and `ttlSec` is a dead-man that self-clears the ring if the controller dies mid-turn. Legacy firmware falls back to controller-streamed frames. Controller `leds` messages carry an explicit `listening: true` flag on listening-ring frames — the device's direction overlay keys off it (pre-scene firmware inferred "listening" from an all-green ring, which breaks for any other scene; the heuristic remains as fallback for old controllers). The direction overlay brightens the base ring colour instead of painting green. Mute ring (red) and volume arc (cyan) are device-local and scene-independent by design.

`server.go` maintains a `ledMode` (direction arc vs. system). System-level LEDs (controller commands, mute ring, pulse animations) always win over the beamformer direction arc. Two paint suppressions in `SetLEDs`/`SetDirectionLEDs` (state is still recorded in `baseLEDs` so the ring can be restored):

- **Mute ring** (solid red) is device-sovereign — enforced since v2.7.8: controller LED writes are recorded but not painted while muted. Needed because muting now terminates an active turn (controller cancels + `speaker_flush` on `mute_state`), so the cancelled turn's LED cleanup arrives after the red ring is up.
- **Volume arc** owns the ring for its 2s display window — turn animations repaint ~every 100ms and would otherwise stomp the arc within one frame. On expiry the ring repaints the latest `baseLEDs` frame (`onDisplayExpire` → `paintBaseLEDs`), handing back mid-animation.

## cgo dependency

SpeexDSP C source (AEC) is vendored in `device/internal/aec/`. The compiler Docker image provides the ARM cross-toolchain. If adding new cgo dependencies, they must compile cleanly with the `echomuse-compiler` image against the FireOS 5 sysroot.
