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

## Building

The Echo Dot runs FireOS 5 (API 22). A custom Docker build environment is required — standard Go cross-compilation won't produce a compatible binary.

**Prerequisites:**
- Docker
- Go 1.24+
- [GoTinyAlsa](https://github.com/Binozo/GoTinyAlsa) cloned to `~/GoTinyAlsa`

**Build the compiler image:**
```bash
docker build -t echomuse-compiler compiler/
```

**Compile:**
```bash
docker run --rm \
  -e CGO_LDFLAGS="-Wl,--hash-style=both" \
  -v "$(pwd)":/sdk \
  -v ~/GoTinyAlsa:/GoTinyAlsa \
  echomuse-compiler
```

Output: `build/server`

---

## VAD configuration

Tunable via environment variables on the device:

| Variable | Default | Description |
|---|---|---|
| `VAD_CHANNEL` | `0` | Mic channel (0–8) |
| `VAD_THRESHOLD` | `0.015` | RMS threshold 0.0–1.0 |
| `VAD_SPEECH_MS` | `80` | Ms of speech to open the gate |
| `VAD_SILENCE_MS` | `600` | Ms of silence to close the gate |

---

## Acknowledgements

- [EchoGo](https://github.com/Binozo/EchoGo) — Binozo
- [GoTinyAlsa](https://github.com/Binozo/GoTinyAlsa) — Binozo
- [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/) — R0rt1z2
- [EchoCLI](https://github.com/Dragon863/EchoCLI) — Dragon863

---

## License

MIT
