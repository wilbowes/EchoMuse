# EchoMuse

Give your Amazon Echo Dot 2nd Generation a second life as a fully local,
open-source voice assistant and media player for Home Assistant.

EchoMuse replaces the Alexa firmware with a lightweight Go server and pairs
it with a Python controller that presents each Dot to Home Assistant as a
native **ESPHome voice satellite** — no cloud, no custom HA integration to
install. Say your wake word, talk to [Assist](https://www.home-assistant.io/voice_control/),
hear the answer through the Dot's speaker. The hardware you already own
($10 on the second-hand market) does the rest.

## What you get

- **Wake word → Assist → spoken response**, fully local. Wake detection runs
  on the controller (openwakeword), so models, sensitivity, and improvements
  never need a firmware update.
- **Custom wake words** — train your own ("hey biscuit") from synthetic TTS
  speech with the bundled [`oww_forge/`](oww_forge/README.md) trainer, then
  install it from the dashboard in one click.
- **Barge-in** — say the wake word over the assistant's own reply to cut it
  off, backed by an on-device echo canceller (vendored speexdsp).
- **Multi-room done right** — one utterance in earshot of two Echos gets
  **one** response: detections are pooled and the best-placed device answers.
- **Music** — each Dot is an HA `media_player` you can actually play things
  on (media browser, Music Assistant, radio streams), with instant
  pause/stop and automatic pause-for-voice, resume-after.
- **Bluetooth proxy** — each Echo doubles as an HA Bluetooth advertisement
  proxy (great with [Bermuda](https://github.com/agittins/bermuda) for room
  presence).
- **Fleet dashboard** — provisioning wizard, per-device or global config
  pushed live (EQ, LED ring scenes, mic tuning), A/B-slot OTA updates with
  automatic fallback, root shell, logs, and per-turn activity analytics
  (wake scores, near-misses, latencies, playback underruns).
- **Encrypted device link** — TLS with a controller-generated CA plus
  per-device tokens; the wizard installs credentials automatically.

The 7-mic array, LED ring, buttons, and speaker are all driven natively:
onset-ratio beamforming, +24dB pre-truncation mic gain (the stock capture
path throws away most of the signal), device-local LED animations, mute
that's genuinely hardware (ADC off, red ring, button LED).

## How it works

```
Echo Dot (Go firmware) ⇄ WebSocket/TLS ⇄ Controller (Python) ⇄ ESPHome native API ⇄ Home Assistant
```

The device is deliberately dumb: it captures, beamforms, and streams audio
continuously, and plays what it's sent. Everything that can drift or
misjudge — wake scoring, endpointing, noise suppression, EQ, arbitration —
lives on the controller where it can be observed and updated fleet-wide.
The full tour is in [docs/voice-pipeline.md](docs/voice-pipeline.md).

This project builds on [EchoGo](https://github.com/Binozo/EchoGo) by Binozo —
the original SDK that made this hardware accessible.

---

## Before you start

Your Echo Dot must be rooted with persistent root. The full rooting guide —
and a detailed engineering journal of how every subsystem was figured out —
is in [`SETUP.md`](SETUP.md).

The short version:
- Persistent unlock via [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/) (R0rt1z2)
- FireOS 5 (Android 5.1, API 22)
- Magisk 17.3
- Alexa voice stack disabled (the dashboard's debloat step handles this)

---

## Running the controller

The controller (dashboard, wake word detection, Home Assistant integration)
ships as a prebuilt Docker image:

```bash
mkdir echomuse && cd echomuse
curl -O https://raw.githubusercontent.com/wilbowes/EchoMuse/main/controller/docker-compose.deploy.yml
curl -o .env https://raw.githubusercontent.com/wilbowes/EchoMuse/main/controller/.env.example
# Edit .env: set SERVER_IP to this machine's LAN IP
docker compose -f docker-compose.deploy.yml up -d
```

Dashboard at `http://<SERVER_IP>:8768`. From there the **provisioning
wizard** takes a rooted Dot the rest of the way: firmware install, TLS
credentials, debloat, and approval into the fleet. Home Assistant then
discovers each device automatically via its built-in ESPHome integration.

See the [quickstart](docs/quickstart.md) for the full walkthrough and
[configuration](docs/configuration.md) for every knob explained in plain
language.

Images are published to `ghcr.io/wilbowes/echomuse-controller` from
`controller-v*` tags; device firmware binaries are released from plain
`v*` tags (see Releases).

---

## Building from source

The Echo Dot runs FireOS 5 (API 22). A custom Docker build environment is
required — standard Go cross-compilation won't produce a compatible binary.

```bash
git submodule update --init          # GoTinyAlsa (wilbowes fork, carries a leak fix)
cd device
docker build -t echomuse-compiler compiler/
./compile.sh                         # output: build/server
```

Controller from source: `cd controller && pip install -r requirements.txt
&& python em_controller.py` (Python 3.12), or `docker compose up --build`.

Tests run on the host and in CI on every push: `go test ./...` under
`device/` (pure-Go logic) and `python -m pytest tests/` under `controller/`.

---

## Custom wake words

[`oww_forge/`](oww_forge/README.md) trains openWakeWord models from
synthetic TTS speech — no voice recordings needed (though you can add real
ones to sharpen accuracy). It's a standalone Docker batch job with a web
UI; the output is a small `.onnx` you upload straight from the dashboard's
Wake word panel, where it appears as a tile next to the stock models.

---

## Acknowledgements

- [EchoGo](https://github.com/Binozo/EchoGo) — Binozo
- [GoTinyAlsa](https://github.com/Binozo/GoTinyAlsa) — Binozo
- [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/) — R0rt1z2
- [EchoCLI](https://github.com/Dragon863/EchoCLI) — Dragon863
- [SpeexDSP](https://gitlab.xiph.org/xiph/speexdsp) — Xiph.Org Foundation (BSD-3-Clause) — vendored echo canceller
- [DTLN](https://github.com/breizhn/DTLN) — Nils L. Westhausen (MIT) — controller-side noise suppression models
- [openWakeWord](https://github.com/dscripka/openWakeWord) — David Scripka — wake word models and training pipeline

---

## License

MIT
