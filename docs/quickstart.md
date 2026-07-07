# EchoMuse Quickstart

EchoMuse turns an Amazon Echo Dot (2nd generation) into a **fully local voice
assistant** — no Amazon account, no cloud, no audio leaving your house. The
Dot becomes a "satellite": its microphones and speaker are driven by a small
server (the **controller**) running on a computer on your network, which in
turn talks to Home Assistant for the actual "turn on the lights" part.

This guide gets you from zero to talking to your Dot. No programming
knowledge needed — where something genuinely technical is unavoidable (the
one-time rooting of the Dot), we point you at the detailed guide instead of
pretending it's easy.

---

## What you need

| Thing | Why |
|---|---|
| Amazon Echo Dot 2nd gen ("biscuit") | The hardware being repurposed. Second-hand ones are cheap. |
| A computer that's always on (a home server, NAS, Raspberry-Pi-class box or better) | Runs the controller. Docker recommended. |
| Home Assistant | Does the actual assistant work: speech-to-text, understanding, text-to-speech. You need a working [Assist pipeline](https://www.home-assistant.io/voice_control/) already set up. |
| A USB cable + a laptop, once | For the one-time unlock/flash of the Dot. |

## Step 1 — Root the Dot (one time, per device)

The Dot ships locked to Amazon's software. Unlocking it involves flashing
modified firmware over USB — it's the only genuinely fiddly part of the
project, it takes an hour or so the first time, and it's fully documented
step-by-step in [SETUP.md](../SETUP.md).

The good news: the dashboard has a **provisioning wizard** (plug the Dot into
your laptop's USB port, open the dashboard in Chrome, follow the steps) that
automates most of the process after the initial unlock.

You only ever do this once per device. Everything afterwards — updates,
configuration, even a remote terminal — happens over WiFi from the dashboard.

## Step 2 — Start the controller

On your always-on computer:

```bash
git clone https://github.com/wilbowes/EchoMuse.git
cd EchoMuse/controller
cp .env.example .env
# Edit .env: set SERVER_IP to this computer's LAN IP address
docker compose up -d --build
```

That's it. The controller is now running two things:

- a **dashboard** at `http://<SERVER_IP>:8768` — your control panel
- a listener that the Dots find automatically on your network (no IP
  configuration needed on the device side)

## Step 3 — Create your admin account

Open `http://<SERVER_IP>:8768` in a browser. On a fresh install you'll see
the Echo graphic with a **pulsing amber ring** and a setup form.

It asks for a **setup token** — this is a one-time code printed in the
controller's logs, so that only you (the person who can read the server's
logs) can claim the controller. Get it with:

```bash
docker logs echomuse-controller
```

Look for the boxed token near the top, paste it in, pick a username and
password, and you're in. From then on the page shows a **green ring** and a
normal login.

## Step 4 — Approve your device

When a rooted Dot powers up, it finds the controller by itself and asks to
join. New devices appear in the dashboard as **pending** — nothing works
until you click **Approve**. (This is deliberate: nothing joins your voice
network without you saying so.)

Once approved, the Dot connects fully: you'll see it as **online**, with its
volume, settings, and a live status.

## Step 5 — Connect it to Home Assistant

The controller makes each Dot look like an **ESPHome voice satellite** —
something Home Assistant already knows how to talk to, with no custom
add-ons:

1. In Home Assistant: **Settings → Devices & Services**. Each EchoMuse
   device is normally **auto-discovered** ("echomuse-…"). If not, add the
   **ESPHome** integration manually with the controller's IP and the port
   shown on the device's dashboard page (16001 for the first device, 16002
   for the second, …).
2. Assign the new device to your Assist pipeline (Settings → Voice
   assistants).

## Step 6 — Talk to it

Say the wake word — **"Hey Rhasspy"** by default (changeable in the
dashboard, see [configuration.md](configuration.md)) — then speak normally:

> "Hey Rhasspy … turn off the kitchen lights."

The LED ring tells you what's happening:

| Ring | Meaning |
|---|---|
| Off | Idle, listening for the wake word |
| Green | Heard the wake word, recording your command |
| Light-green segment | Which direction it thinks you're speaking from |
| Spinning | Thinking (Home Assistant is processing) |
| Solid red | Microphones muted (the physical mute button — hardware-level since v2.7.4) |

## Everyday things

- **Updates**: when a new EchoMuse release is out, the dashboard shows an
  update badge — one click updates the device over WiFi. If an update ever
  goes wrong, the device automatically rolls back to its previous version.
- **Settings**: everything tunable lives in the dashboard, either fleet-wide
  (the gear icon) or per device. See [configuration.md](configuration.md).
- **Terminal**: each device page has a full remote terminal (for the
  curious; you never *need* it).
- **Volume**: buttons on the Dot, the dashboard slider, or Home Assistant's
  media player card — they all stay in sync.

## When something doesn't work

1. Is the device **online** in the dashboard?
2. Does the wake word register? The Status tab shows recent wake detections
   and "near-misses" (times it almost triggered) — if you're getting
   near-misses, nudge the sensitivity up a step (see configuration.md).
3. Bad transcriptions? See the microphone section of
   [voice-pipeline.md](voice-pipeline.md) — room noise and speaker distance
   are the usual suspects.
4. The troubleshooting section of [SETUP.md](../SETUP.md) covers the deeper
   stuff.
