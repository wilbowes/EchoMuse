# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

EchoMuse repurposes Amazon Echo Dot Gen 2 (FireOS 5 / Android 5.1, codename "biscuit") as an open-source voice assistant satellite. Two components:

- **`device/`** — Go binary that runs directly on the rooted Echo Dot
- **`controller/`** — Python asyncio WebSocket server that manages devices, runs wake word detection, and proxies to a voice pipeline

## Building the device binary

The Echo Dot runs FireOS 5 (API 22). Standard Go cross-compilation won't work — a custom Docker build environment is required.

**One-time setup:**
```bash
# GoTinyAlsa is a git submodule at the repo root
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
cp .env.example .env   # fill in SERVER_IP and VOICE_WS_URI
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
- `VOICE_MODE` — `claracore` (default) or `esphome`; changing it requires a controller restart
- `VOICE_WS_URI` — WebSocket URI of the downstream voice server (claracore mode only)
- `OWW_MODEL` / `OWW_THRESHOLD` — OpenWakeWord model name and detection threshold
- `DEVICE_APPROVAL` — `strict` (admin must approve new devices) or `auto`

### Voice backend modes

- **claracore** — controller streams the voice turn to `VOICE_WS_URI` over WebSocket and plays back the PCM response (`run_voice_turn()`).
- **esphome** — controller impersonates ESPHome voice satellites: one asyncio TCP listener per device on ports 16001+ (persisted in the device registry, never reused). Home Assistant's built-in ESPHome integration dials in and drives voice turns via Assist. Implemented in `em_esphome.py` on top of the protocol layer in `controller/esphome/` (`frame_protocol.py`, `satellite_server.py`, vendored aioesphomeapi protobufs in `esphome/vendor/`).

## Architecture

### Device → Controller protocol

Each device opens **three** WebSocket connections to the controller:

| Path | Direction | Purpose |
|------|-----------|---------|
| `/control` | bidirectional JSON | Registration, LEDs, mic_start/stop, button events, config push |
| `/data` | binary | Mic PCM frames in (0x01 header), speaker PCM frames out (0x02/0x03) |
| `/shell/{device_id}` | raw binary | Root shell proxy (demand-opened by device on `shell_open` command) |

Controller is discovered by the device via mDNS (`_emcontroller._tcp.local`).

### Device audio pipeline

Each 32ms period passes through, in order:

```
raw 9ch S24_3LE → beamformer + fixed mic gain (micGainDb, applied to 24-bit samples) → mono S16_LE → [AEC] → RNNoise NS → [AGC] → [VAD gate] → /data WebSocket
```

The always-on wake stream (`mic_start` without `lock_mic`) is **ungated and AGC-free**: every 32ms period is sent continuously (batched into 80ms frames) so openwakeword scores an uninterrupted stream, and no adaptive gain state can drift with room noise. The VAD gate and AGC apply only to bounded `lock_mic` turn streams (button-triggered), which get a fresh `ResetAGC()` per stream.

- **Beamformer** (`internal/beamformer/`) — selects the perimeter mic with the highest onset energy ratio (fast/slow EWMA) at voice turn start, then locks for the duration. Its `extractChannel` also applies the fixed mic gain (`micGainDb`, default +24dB) against the full 24-bit sample before quantising to S16 — captured speech sits at ~−70dBFS, so gain must happen pre-truncation to recover real resolution. `vadThreshold` stays in pre-gain units (the device scales it by the gain internally)
- **AEC** (`internal/aec/`) — speexdsp echo canceller (vendored C, SpeexDSP-1.2.1), whole mic path including the wake stream; far-end reference tapped at the speaker ALSA write (every period incl. silence, same codec clock → no drift), delayed by `aecDelayMs`. Default off (`aecEnabled`)
- **RNNoise** (`internal/rnnoise/`) — vendored C source (xiph/rnnoise v0.1), compiled via cgo; no external library required
- **AGC** (`internal/processor/`) — lock_mic turns only; release is frozen during silence and when RNNoise speech probability < 0.5, preventing noise floor amplification
- **VAD** (lock_mic turns only) runs on pre-NS/AGC audio; opens gate after `VAD_SPEECH_MS` of speech, closes after `VAD_SILENCE_MS` of silence, then sends an end-of-speech sentinel

### Controller audio pipeline

1. **Wake word** — openwakeword (ONNX) runs in a thread executor per device on `mic_queue`
2. **Voice turn** — on wake or dot-button: drain stale frames → acquire `voice_lock` → stream mic to the voice backend (ClaraCore WebSocket or ESPHome/HA, per `VOICE_MODE`) → receive PCM response → EQ (`em_eq.py`) → resample to 48kHz stereo → stream back as 0x02 frames
3. **Speaker** — `resample_to_stereo_48k()` uses numpy linear interpolation (not pure Python)

### Key Go packages

| Package | Role |
|---------|------|
| `cmd/server.go` | Entry point: wires hardware, callbacks, and clients together |
| `internal/client/control.go` | WebSocket client to controller `/control` — registration, message dispatch |
| `internal/client/data.go` | WebSocket client to controller `/data` — mic streaming, speaker playback |
| `internal/server/` | Local state machine: mute, volume, LED mode priority |
| `internal/config/config.go` | Global runtime config; env var defaults, overridden by controller push |
| `internal/bindings/` | Hardware drivers: mic PCM, speaker PCM, LED I2C, button evdev |
| `pkg/led/`, `pkg/mic/`, `pkg/speaker/`, `pkg/buttons/` | Hardware abstractions (interfaces) |

### Key Python modules

| File | Role |
|------|------|
| `em_controller.py` | WebSocket server, `Device` registry, voice pipeline, mDNS |
| `em_api.py` | aiohttp HTTP API + dashboard SPA, OTA, shell proxy |
| `em_db.py` | SQLite persistence (devices, config, logs, users) |
| `em_auth.py` | Session auth with bcrypt |
| `em_eq.py` | Parametric EQ applied to TTS audio before playback |
| `em_esphome.py` | ESPHome-mode satellite servers (`EchoMuseSatellite`, `DeviceESPhomeServer`) |
| `esphome/` | ESPHome native API protocol layer (framing, handshake, vendored protobufs) |

## OTA update system

The device runs an A/B slot binary system:
- `/data/local/bin/server` is a symlink to either `server_a` or `server_b`
- `start_server.sh` counts fast exits (< 15s runtime); after 3 consecutive failures it flips the symlink to the other slot and exits, letting Android init restart with the fallback binary

OTA is triggered from the dashboard — the controller pushes the new binary via the `/shell` WebSocket.

## Device config push

`config.ConfigMessage` JSON fields (camelCase) are sent from controller to device on connect and on per-device config change. Non-zero fields are applied; zero/nil fields are ignored (partial update). Changes take effect immediately — no restart required.

Configurable parameters: `vadThreshold`, `vadSpeechMs`, `vadSilenceMs`, `owwThreshold`, `owwModel`, `adcDigitalGain`, `adcMicpga`, `micGainDb`, `startupVolume`, `beamAngle`, `beamformingEnabled`, `aecEnabled`, `aecDelayMs`, `aecTailMs`, `bargeInEnabled`, `bargeInThreshold`.

## LED priority system

`server.go` maintains a `ledMode` (direction arc vs. system). System-level LEDs (controller commands, mute ring, pulse animations) always win over the beamformer direction arc. The mute ring (solid red) is device-sovereign — it cannot be overridden by controller LED commands while muted.

## cgo dependency

RNNoise C source is vendored in `device/internal/rnnoise/src/`. The compiler Docker image provides the ARM cross-toolchain. If adding new cgo dependencies, they must compile cleanly with the `echomuse-compiler` image against the FireOS 5 sysroot.
