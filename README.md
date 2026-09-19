# EchoMuse

**Turn an old Amazon Echo Dot (2nd gen) into a local voice assistant for Home Assistant.**

[![CI](https://github.com/wilbowes/EchoMuse/actions/workflows/ci.yml/badge.svg)](https://github.com/wilbowes/EchoMuse/actions/workflows/ci.yml)
[![Firmware](https://img.shields.io/github/v/release/wilbowes/EchoMuse?filter=v*&label=firmware)](https://github.com/wilbowes/EchoMuse/releases)
[![emOS](https://img.shields.io/github/v/release/wilbowes/EchoMuse?filter=emos-v*&label=emOS)](https://github.com/wilbowes/EchoMuse/releases)
[![Controller](https://img.shields.io/github/v/tag/wilbowes/EchoMuse?filter=controller-v*&label=controller)](https://github.com/wilbowes/EchoMuse/pkgs/container/echomuse-controller)
[![License: MIT](https://img.shields.io/github/license/wilbowes/EchoMuse)](LICENSE)

<!-- Demo video goes here. -->

EchoMuse makes a second-hand Echo Dot a voice satellite for
[Home Assistant](https://www.home-assistant.io/voice_control/): say the wake
word, ask something, and hear the answer from the Dot. There is no Amazon
account, no Alexa and no cloud service of ours. The Dot shows up in Home
Assistant as an ordinary ESPHome voice satellite, so there is nothing extra to
install there.

## At a glance

**What it is**

- A replacement for the software on an **Echo Dot 2nd generation (2016)**. Its
  microphones, speaker, LED ring and buttons all work.
- A **controller** you run on your own network, as a Home Assistant add-on or
  a Docker container. It detects the wake word, manages your devices and has
  a web dashboard.
- A way to reuse hardware that sells second-hand for very little.

**What it isn't**

- **Not Alexa.** No Alexa skills, shopping, calling, Drop In or Amazon
  account. The assistant is Home Assistant's Assist, so it can do what your
  Home Assistant can do.
- **Not standalone.** You need a working Home Assistant with an
  [Assist pipeline](https://www.home-assistant.io/voice_control/). Home
  Assistant does the speech recognition, the understanding and the voice.
- **Not ready out of the box.** The Dot needs a one-time unlock over USB with
  R0rt1z2's tool first. Follow the instructions and it is straightforward. If
  a step goes wrong the Dot can end up soft-bricked, and the
  [XDA thread](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/)
  covers recovering it.
- **Not for other Echo models, yet.** Only the Echo Dot 2nd gen works today.
  Ports to the Echo Dot 3, Echo 2 and Echo Show 8 are in progress with
  community help.
- **Not affiliated with Amazon.**

## What you need

| | |
|---|---|
| An Echo Dot 2nd gen | The hardware being repurposed. |
| Home Assistant | With a working Assist pipeline. |
| An always-on computer | Runs the controller. The Home Assistant machine itself is fine if it runs add-ons. |
| A Linux computer (a live USB works) and a micro-USB cable, once | To unlock the Dot. The unlock does not run on macOS. |
| Chrome or Edge, once | The setup wizard talks to the Dot over USB from the browser. |

## Getting started

1. **Unlock the Dot** with R0rt1z2's
   [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/).
   Either version works: v1.1.0 leaves it on FireOS 5, v2.0.0 moves it to
   FireOS 6. The [rooting guide](docs/rooting.md) explains the choice. **You
   do this at your own risk.**
2. **Start the controller**, as an add-on:

   [![Add the EchoMuse repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fwilbowes%2FEchoMuse)

   or with Docker:

   ```bash
   mkdir echomuse && cd echomuse
   curl -O https://raw.githubusercontent.com/wilbowes/EchoMuse/main/controller/docker-compose.deploy.yml
   curl -o .env https://raw.githubusercontent.com/wilbowes/EchoMuse/main/controller/.env.example
   docker compose -f docker-compose.deploy.yml up -d
   ```
3. **Run the setup wizard** from the dashboard with the Dot plugged in over
   USB. It installs everything and joins your WiFi. Approve the Dot in the
   dashboard and Home Assistant discovers it.

The [quickstart](docs/quickstart.md) walks through all three.

### emOS or FireOS?

The wizard offers two ways to run EchoMuse on the Dot:

- **emOS** (the default) replaces Amazon's Android with our own small Linux
  userspace, keeping only the Dot's kernel. It runs on FireOS 5 and FireOS 6
  devices, and the 3.5mm jack works properly on it. The wizard keeps a copy of
  your original boot image, and restoring it takes about ten seconds.
- **FireOS with root** keeps Amazon's Android 5 and runs EchoMuse on top. FireOS
  5 only. It is the older path, with the most hours behind it.

## Features

- **Voice turns through Assist**, with the answer played on the Dot.
- **Wake word on the controller or on the Dot.** By default the controller
  listens for the wake word. The Dot can do it itself instead: switch it on
  in the dashboard's wake word settings, for every Dot or just one. If a Dot can't (older firmware, or its wake
  word model isn't installed yet), the controller takes over, so it never
  goes deaf.
- **Barge-in:** say the wake word over a reply to interrupt it.
- **More than one Dot:** the first to hear the wake word answers and the rest
  stay quiet.
- **Music:** each Dot is a Home Assistant media player (media browser, Music
  Assistant, radio). Speaking over music lowers it under the answer rather
  than pausing it.
- **Timers and announcements** from Home Assistant.
- **Custom wake words** you train yourself with [`oww_forge`](oww_forge/README.md)
  and install from the dashboard.
- **Extras in Home Assistant:** a Bluetooth proxy per Dot (handy with
  [Bermuda](https://github.com/agittins/bermuda)), an ambient light sensor on
  Dots that have one, and the action button as an event you can automate.
- **A dashboard** for setup, updates, per-device settings (EQ, LED ring, mic
  tuning), logs and a history of voice turns.
- **Firmware updates over WiFi**, with automatic rollback if a new version
  fails to start. emOS itself is updated by re-running the wizard, for now
  ([#573](https://github.com/wilbowes/EchoMuse/issues/573)).

## Privacy

- **Nothing leaves your network because of EchoMuse.** The Dot streams its
  microphone to the controller on your LAN, which listens for the wake word.
  Only after the wake word is audio passed to Home Assistant, and where it
  goes from there depends on your Assist pipeline (fully local with Whisper
  and Piper, or a cloud service if you chose one).
- **No telemetry.** No analytics, no install counter. The controller only
  connects out to GitHub, to check for and download releases, and you can set
  how often it checks ([details](docs/configuration.md#what-leaves-your-network)).
- **The mute button is a software mute.** It silences the microphones in the
  audio chip and EchoMuse refuses to listen while it is on, but the Dot 2 has
  no hardware switch that disconnects them.
- **Even with wake word on the Dot, the microphone still streams to the
  controller**, which keeps listening for comparison and for barge-in.
  Keeping all audio on the Dot until it hears the wake word is the next step
  ([#207](https://github.com/wilbowes/EchoMuse/issues/207)).

## Status and known issues

EchoMuse is in active development and runs daily on a small fleet. emOS is
newer than FireOS and has fewer device-hours behind it. Open issues worth
knowing before you start:

- The 3.5mm jack: unplugging can stall the microphone for about 30 seconds ([#117](https://github.com/wilbowes/EchoMuse/issues/117),
  [#141](https://github.com/wilbowes/EchoMuse/issues/141)).
- emOS on FireOS 6 cannot join WPA3 networks: the WiFi driver has no support
  for it. Use WPA2 ([#536](https://github.com/wilbowes/EchoMuse/issues/536)).
- An announcement during a ringing timer can't be heard
  ([#373](https://github.com/wilbowes/EchoMuse/issues/373)).

Everything else is in the [issue tracker](https://github.com/wilbowes/EchoMuse/issues).

## Getting help

- **Questions and bugs about EchoMuse go to
  [our issues](https://github.com/wilbowes/EchoMuse/issues), not to the XDA
  thread.** The thread is for the unlock only.
- Check the [FAQ](docs/faq.md) first.
- Attach a support bundle (Dashboard → Support → Download bundle). It holds
  the logs and versions we need, with transcripts, recordings and network
  names removed.

## Documentation

| | |
|---|---|
| [Quickstart](docs/quickstart.md) | From nothing to talking to your Dot. |
| [Rooting](docs/rooting.md) | Unlocking the Dot, and which amonet version to use. |
| [Configuration](docs/configuration.md) | Every setting, in plain language. |
| [FAQ](docs/faq.md) | Common problems and their fixes. |
| [emOS](emos/README.md) | How our own userspace on the Dot works. |
| [How the voice pipeline works](docs/voice-pipeline.md) | The path from wake word to answer. |
| [Device ↔ controller protocol](docs/device-controller-interface.md) | For porting EchoMuse to new hardware. |
| [Contributing](CONTRIBUTING.md) | Building from source, tests, and how to send changes. |
| [Engineering journal](JOURNAL.md) | How each part was worked out, including the dead ends. |

## How it's built

EchoMuse is written largely with [Claude](https://www.anthropic.com/claude),
Anthropic's AI model, directed by the maintainer. More than half
the commits carry a `Co-Authored-By: Claude` line, and replies on our issues
are signed "Team EchoMuse (powered by Claude)". Nothing about that is hidden.

## Credits

EchoMuse would not exist without:

- **R0rt1z2**, for [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/),
  the unlock that everything here depends on.
- **Binozo**, for [EchoGo](https://github.com/Binozo/EchoGo), the original SDK
  for this hardware, and [GoTinyAlsa](https://github.com/Binozo/GoTinyAlsa).
- **Dragon863**, for [EchoCLI](https://github.com/Dragon863/EchoCLI).
- **David Scripka**, for [openWakeWord](https://github.com/dscripka/openWakeWord).
- **Xiph.Org**, for [SpeexDSP](https://gitlab.xiph.org/xiph/speexdsp) (the echo
  canceller), and **Nils L. Westhausen**, for [DTLN](https://github.com/breizhn/DTLN)
  (noise suppression).
- **Home Assistant and ESPHome**, whose open voice stack EchoMuse plugs into.
- Everyone who has [contributed code](https://github.com/wilbowes/EchoMuse/graphs/contributors),
  filed issues or tested on their own hardware.

## Licence

EchoMuse is [MIT licensed](LICENSE). It includes third-party components that
keep their own licences, listed with their copyright notices in
[NOTICE.md](NOTICE.md). emOS releases also ship busybox (GPL-2.0), with its
source and licence attached to each release, and wpa_supplicant (BSD).

Amazon, Echo, Echo Dot, Alexa and FireOS are trademarks of Amazon.com, Inc. or
its affiliates. EchoMuse is an independent project, not affiliated with or
endorsed by Amazon.
