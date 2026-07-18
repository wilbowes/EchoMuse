# EchoMuse

Give your Amazon Echo Dot 2nd Generation a second life as an open source voice assistant satellite.

EchoMuse replaces the Alexa firmware with a lightweight Go server that streams audio to and from a backend controller of your choosing. The LED ring, microphone array, buttons, and speaker remain fully accessible via HTTP API. Pair it with Home Assistant, a local LLM pipeline, or any backend you like.

This is a significant fork of [EchoGo](https://github.com/Binozo/EchoGo) by Binozo — the original SDK that made this hardware accessible. Substantial modifications have been made including client-side VAD, mute button handling, volume control, and audio fixes specific to the biscuit hardware.

---

## Before you start

Your Echo Dot must be rooted with persistent root before EchoMuse is useful. The full rooting and setup guide — including SELinux bypass, Alexa removal, audio configuration, VAD, and wake word detection — is in [`SETUP.md`](SETUP.md).

The short version:
- Persistent unlock via [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/) (R0rt1z2)
- FireOS 5 (Android 5.1, API 22)
- Magisk 17.3
- Alexa voice stack disabled

---

## Running the controller

The controller (dashboard, wake word detection, Home Assistant integration) ships as a prebuilt Docker image:

```bash
mkdir echomuse && cd echomuse
curl -O https://raw.githubusercontent.com/wilbowes/EchoMuse/main/controller/docker-compose.deploy.yml
curl -o .env https://raw.githubusercontent.com/wilbowes/EchoMuse/main/controller/.env.example
# Edit .env: set SERVER_IP to this machine's LAN IP
docker compose -f docker-compose.deploy.yml up -d
```

Dashboard at `http://<SERVER_IP>:8768`. See the [quickstart](docs/quickstart.md) for the full walkthrough.

The device link is encrypted and authenticated (TLS with a controller-generated CA + per-device tokens): the provisioning wizard installs credentials automatically, and existing devices upgrade with the **Secure link** button on their Status tab — see [configuration](docs/configuration.md#encrypted-device-link).

Images are published to `ghcr.io/wilbowes/echomuse-controller` from `controller-v*` tags; device firmware binaries are released from plain `v*` tags (see Releases).

---

## Building the device binary

The Echo Dot runs FireOS 5 (API 22). A custom Docker build environment is required — standard Go cross-compilation won't produce a compatible binary.

**Prerequisites:**
- Docker
- Go 1.24+

GoTinyAlsa is a git submodule at the repo root (the
[wilbowes fork](https://github.com/wilbowes/GoTinyAlsa), which carries a
memory-leak fix not yet upstream):

```bash
git submodule update --init
```

**Build the compiler image:**
```bash
cd device
docker build -t echomuse-compiler compiler/
```

**Compile:**
```bash
cd device
./compile.sh
```

Output: `build/server`

---

## Audio processing pipeline

Each microphone buffer (a 160ms batch of 32ms periods — the ALSA reader delivers whole buffers) passes through a processing chain before VAD gating and transmission:

```
raw 9ch S24_3LE → beamformer (mic selection) → RNNoise NS → AGC → mono S16_LE → VAD gate
```

**Beamformer** locks to the perimeter mic with the highest onset energy ratio at voice turn start. Onset ratio (fast/slow EWMA) is robust to continuous background noise — it picks the direction that just got louder, not the loudest direction overall.

**RNNoise** (xiph/rnnoise v0.1) suppresses background noise. Vendored C source compiled via cgo — no external library or system dependency required.

**AGC** normalises level for consistent wake word and STT input. Release is frozen during silence to prevent noise floor amplification past the VAD threshold.

**VAD** runs on pre-NS/AGC audio to keep threshold calibrated to mic sensitivity rather than processed level.

---

## VAD configuration

Tunable via environment variables on the device (overridden by controller config push):

| Variable | Default | Description |
|---|---|---|
| `VAD_CHANNEL` | `0` | Mic channel for vad_stream endpoint (0–8) |
| `VAD_THRESHOLD` | `0.015` | RMS threshold 0.0–1.0 (controller pushes 0.003) |
| `VAD_SPEECH_MS` | `80` | Ms of speech to open the gate |
| `VAD_SILENCE_MS` | `600` | Ms of silence to close the gate |

---

## Custom wake words

`oww_forge/` trains custom openWakeWord models ("hey biscuit", …) from
synthetic TTS speech — no voice recordings needed. It's a standalone Docker
batch job, separate from the controller; the output is a small `.onnx` the
controller loads by file path. See [`oww_forge/README.md`](oww_forge/README.md).

---

## Acknowledgements

- [EchoGo](https://github.com/Binozo/EchoGo) — Binozo
- [GoTinyAlsa](https://github.com/Binozo/GoTinyAlsa) — Binozo
- [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/) — R0rt1z2
- [EchoCLI](https://github.com/Dragon863/EchoCLI) — Dragon863
- [RNNoise](https://github.com/xiph/rnnoise) — Xiph.Org Foundation (BSD-3-Clause)

---

## License

MIT
