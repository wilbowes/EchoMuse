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
- **On-device wake word (experimental)** — the Echo can run the wake model
  itself and report what it *would* have detected, without acting on it, so
  the two can be compared on identical audio before anything depends on it.
  Off by default; see [docs/configuration.md](docs/configuration.md).
- **Barge-in** — say the wake word over the assistant's own reply to cut it
  off, backed by an on-device echo canceller (vendored speexdsp).
- **Multi-room done right** — one utterance in earshot of two Echos gets
  **one** response: detections are pooled and the best-placed device answers.
- **Music** — each Dot is an HA `media_player` you can actually play things
  on (media browser, Music Assistant, radio streams), with instant
  pause/stop. Speaking over music **ducks** it rather than pausing it: the
  bed drops under the answer and comes back up, so nothing is lost and a
  non-seekable stream doesn't skip the seconds a turn took.
- **Bluetooth proxy** — each Echo doubles as an HA Bluetooth advertisement
  proxy (great with [Bermuda](https://github.com/agittins/bermuda) for room
  presence).
- **Sensors and buttons in HA** — most Dots have an ambient light sensor that
  Amazon's software never exposed; it turns up as a lux sensor, reported
  immediately when a light goes on rather than on a slow poll. Not every
  hardware revision has it fitted, and a device without one simply doesn't
  get the sensor. Holding the action button fires an event you can trigger
  automations from, while a normal press still starts a voice turn — or fires
  its own event instead, if you'd rather bind the tap. The hold keeps working
  with the mic muted, so a Dot muted for privacy is still a button.
- **Headphones** — plug into the 3.5mm jack and audio moves there, unplug and
  it comes back, no reboot needed.
- **Fleet dashboard** — provisioning wizard, per-device or global config
  pushed live (EQ, LED ring scenes, mic tuning), A/B-slot OTA updates with
  automatic fallback, root shell, logs, and per-turn activity analytics
  (wake scores, near-misses, latencies, playback underruns). Optionally keep
  the last few turns' mic audio to play back — the only honest way to judge
  capture quality and tune gain by ear rather than by inference.
- **Encrypted device link** — TLS with a controller-generated CA plus
  per-device tokens; the wizard installs credentials automatically.
- **No phone-home** — there is no telemetry, no analytics and no install
  counter. Nobody, including us, can tell how many people run EchoMuse or
  which features they use, and that is deliberate. The controller's only
  outbound connection is an hourly check to GitHub's API for a newer
  release, so the dashboard can tell you one exists — the same exposure as
  a `git clone`, and how often it happens is yours to set
  ([docs/configuration.md](docs/configuration.md#what-leaves-your-network)).

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
(The one exception is opt-in and observational: the Echo can *also* score the
wake word locally and report what it would have detected, which is how we're
measuring whether that belongs on the device at all.)
The full tour is in [docs/voice-pipeline.md](docs/voice-pipeline.md).

The two halves version independently, so any pairing of firmware and
controller has to work. What a device can be asked to do is negotiated by
**capability**, not by comparing version numbers — see
[Compatibility](#compatibility).

This project builds on [EchoGo](https://github.com/Binozo/EchoGo) by Binozo —
the original SDK that made this hardware accessible.

---

## Before you start

**New here? Start with the [quickstart](docs/quickstart.md)** — it's the
guided path from zero to talking to your Dot, and it sends you to the
rooting guide at the right moment rather than opening with it.

Your Echo Dot must be rooted with persistent root. That guide — along with a
detailed engineering journal of how every subsystem was figured out — is in
[`SETUP.md`](SETUP.md) for how the hardware works, [`JOURNAL.md`](JOURNAL.md)
for the build log, and [rooting](docs/rooting.md) to prepare a device — none
of which is a
walkthrough.

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
# Optional: set SERVER_IP to this machine's LAN IP (detected if left empty)
docker compose -f docker-compose.deploy.yml up -d
```

### Or, as a Home Assistant add-on

If Home Assistant runs the Supervisor (HA OS, or Supervised), install this
repository as an add-on repository and add "EchoMuse" from the Add-on Store
— no separate Docker host needed.

[![Open your Home Assistant instance and show the add add-on repository dialog with this repository pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fwilbowes%2FEchoMuse)

Open the dashboard — `http://<SERVER_IP>:8768` for the Docker install, or the
add-on's **Open Web UI** button / sidebar panel for the add-on install. From
there the **provisioning
wizard** takes a stock Dot the rest of the way over USB: root, debloat,
WiFi, firmware, TLS credentials and the on-device wake word assets. It ends
by rebooting the Dot, which then finds the controller itself and appears as
pending for you to approve. Home Assistant discovers each approved device
automatically via its built-in ESPHome integration.

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

## Compatibility

Device firmware (`v*` tags) and the controller (`controller-v*` tags) are
released independently, so at any moment you may be running new firmware
against an older controller or the reverse — during a staged rollout, that is
guaranteed. Two rules keep that safe:

**Features are negotiated by capability, not version.** On connect, a device
announces what it implements (`mic`, `speaker`, `leds`, `led_anim`, `buttons`,
`oww_shadow`). The controller asks "does this device say it can?" rather than
"is its version at least X" — because the latter means encoding release
history into the controller, and it gets a dev build wrong immediately. A
control that depends on a capability the device lacks is shown disabled with
the reason, never as a control that silently does nothing. A test asserts the
capability strings match across the Go and Python sources, because a typo
there makes a feature permanently unavailable while looking exactly like
unsupported hardware.

**Both directions degrade to the old behaviour, never to a wrong answer.**
Unknown JSON fields and unknown message types are ignored, so neither side
breaks on data it does not understand. Where a new field records a
measurement, its absence is stored as *no data* rather than as zero — an old
device reporting no playback statistics must not read as "zero underruns",
and one that cannot score wake words locally must not read as "scored and
missed every time". That distinction is why several columns are nullable and
why some carry a companion flag saying whether the device was even capable of
producing them.

## Acknowledgements

- [EchoGo](https://github.com/Binozo/EchoGo) — Binozo
- [GoTinyAlsa](https://github.com/Binozo/GoTinyAlsa) — Binozo
- [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/) — R0rt1z2
- [EchoCLI](https://github.com/Dragon863/EchoCLI) — Dragon863
- [SpeexDSP](https://gitlab.xiph.org/xiph/speexdsp) — Xiph.Org Foundation (BSD-3-Clause) — vendored echo canceller
- [DTLN](https://github.com/breizhn/DTLN) — Nils L. Westhausen (MIT) — controller-side noise suppression models
- [openWakeWord](https://github.com/dscripka/openWakeWord) — David Scripka — wake word models and training pipeline

---

## Contributing

Bug reports, fixes and hardware findings are all welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md). The most useful thing you can send is an
issue with a support bundle attached (Dashboard → Support → Download bundle);
it carries the logs, versions and metrics needed to diagnose something
remotely, with transcripts, recordings and network names excluded.

---

## License

MIT — see [LICENSE](LICENSE).

EchoMuse vendors and links several third-party components, each keeping its own
licence. They are inventoried in [NOTICE.md](NOTICE.md); note that the device
binary links two BSD-3-Clause components, whose copyright notices that file
carries on the binary's behalf.
