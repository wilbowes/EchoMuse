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
step-by-step in [rooting](rooting.md), which points at R0rt1z2's XDA Forums
thread for the exploit itself.

The good news: the dashboard has a **provisioning wizard** (plug the Dot into
your laptop's USB port, open the dashboard in Chrome, follow the steps) that
automates most of the process after the initial unlock.

If a step fails, the wizard offers a **Download diagnostics** file to attach
to an issue. It captures the device's state at the moment it failed, which
saves a round trip of being asked to run things by hand. If you unplug the
device at any point you can carry on, because **Reconnect** is on every step.
Note that the cable is the Dot's only power, so unplugging reboots it and it
comes back in Android. The wizard will say so if the step you are on needed
recovery mode.

You only ever do this once per device. Everything afterwards — updates,
configuration, even a remote terminal — happens over WiFi from the dashboard.

## Step 2 — Start the controller

Two ways, and they run the same software. If you already have Home
Assistant, the add-on is less work; otherwise use Docker on any
always-on machine.

<details open>
<summary><b>As a Home Assistant add-on</b></summary>

Settings → Add-ons → Add-on Store → ⋮ → **Repositories**, paste
`https://github.com/wilbowes/EchoMuse`, then install **EchoMuse** from the
store. The README has a one-click badge for adding the repository.

The dashboard appears as a **sidebar panel** — it is reached through Home
Assistant, so there is no extra port to open and no second address to
remember. It is *only* reachable that way: the add-on refuses connections
that do not arrive through Home Assistant.

Settings that would go in `.env` are add-on options instead (Configuration
tab). Leave `server_ip` empty unless the controller picks the wrong
address — see [When something doesn't work](#when-something-doesnt-work).

Your data lives in the add-on's own storage and survives updates.

**If you have devices from a previous controller**, they hold that
controller's certificate authority and will refuse to trust a new one.
Copy the old `data/tls/` directory (all four files) into the add-on's
data directory before connecting them, or they cannot connect at all.

</details>

<details>
<summary><b>With Docker, on any always-on computer</b></summary>

Using the prebuilt image (nothing to compile):

```bash
mkdir echomuse && cd echomuse
curl -O https://raw.githubusercontent.com/wilbowes/EchoMuse/main/controller/docker-compose.deploy.yml
curl -o .env https://raw.githubusercontent.com/wilbowes/EchoMuse/main/controller/.env.example
# Optional: set SERVER_IP in .env to this computer's LAN IP. Left empty it is
# detected, and the address in use is printed at startup.
docker compose -f docker-compose.deploy.yml up -d
```

To upgrade later: `docker compose -f docker-compose.deploy.yml pull && docker compose -f docker-compose.deploy.yml up -d`. Your devices, users, and settings live in `./data` and survive upgrades.

<details>
<summary>Alternative: build from source (needed for NVIDIA GPU wake-word inference)</summary>

```bash
git clone https://github.com/wilbowes/EchoMuse.git
cd EchoMuse/controller
cp .env.example .env
# Optional: set SERVER_IP to this computer's LAN IP. Left empty it is detected.
docker compose up -d --build
```

Note: `docker-compose.yml` requests an NVIDIA GPU (for onnxruntime-gpu).
On a machine without one, remove the `deploy:` block and the `GPU: "1"`
build arg — or just use the prebuilt image above, which is CPU-only.

</details>

</details>

That's it. The controller is now running two things:

- a **dashboard** at `http://<SERVER_IP>:8768` — your control panel
- a listener that the Dots find automatically on your network (no IP
  configuration needed on the device side)

## Step 3 — Create your admin account

**On the Home Assistant add-on** there is nothing to create. Open the
**EchoMuse panel** in the sidebar and you are already signed in as your Home
Assistant user — it has authenticated you, so a second password would be a
lock on a door that is already locked. The first person to open the panel
becomes the EchoMuse admin; anyone after that gets read-only access until an
admin promotes them under **Settings → Users**.

Roles are EchoMuse's own and are **not** copied from Home Assistant — being an
HA administrator does not make you an EchoMuse one. Read-only is a real
restriction rather than a formality: recordings and the transcript text of a
turn are admin-only, because reaching this dashboard is not the same as being
trusted with speech from inside the house.

**With Docker**, open `http://<SERVER_IP>:8768`. On a fresh install you'll
see the Echo graphic with a **pulsing amber ring** and a setup form.

It asks for a **setup token** — a one-time code printed in the controller's
logs, so that only you (the person who can read the server's logs) can claim
the controller:

```bash
docker logs echomuse-controller
```

Look for the boxed token near the top, paste it in, pick a username and
password, and you're in. From then on the page shows a **green ring** and a
normal login.

## Step 4 — Approve your device

When a rooted Dot powers up, it finds the controller by itself and asks to
join. New devices appear in the dashboard as **pending** — nothing works
until you give it a name and click **Approve & Add to Fleet**. (This is
deliberate: nothing joins your voice network without you saying so.)

Once approved, the Dot connects fully: you'll see it as **online**, with its
volume, settings, and a live status.

## Step 5 — Connect it to Home Assistant

The controller makes each Dot look like an **ESPHome voice satellite** —
something Home Assistant already knows how to talk to, with no custom
add-ons:

1. In Home Assistant: **Settings → Devices & Services → Add Integration →
   ESPHome**, then enter the **controller's IP** and the device's **port**:
   16001 for the first device, 16002 for the second, and so on (each
   device's port is shown on its dashboard page). One integration entry per
   device.
2. Assign the new device to your Assist pipeline (Settings → Voice
   assistants).

The device appears in HA as **`<name> Voice Assistant`** (e.g. "Lounge
Voice Assistant"), with Model "Echo Dot Gen 2 (biscuit)" — the Bluetooth
proxy, if enabled, shows up separately as `<name> BT Proxy`.

> **Auto-discovery:** if Home Assistant runs on the **same subnet** as the
> controller, devices should also pop up automatically as discovered
> "echomuse-…" entries (fixed in v2.7.5 — earlier versions advertised
> incompletely and HA silently ignored them, so manual entry was the only
> way). Devices you've already added manually won't re-appear as
> discoveries — HA knows it has them. If HA lives on a **different subnet
> or VLAN**, discovery can't cross that boundary (it uses local-only
> multicast) and manual entry remains the normal path — still a one-time,
> 30-second job per device.

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
| Cyan arc | Volume level, shown for 2 seconds after a volume press (even mid-response) |
| Solid red | Microphones muted (the physical mute button — hardware-level since v2.7.4). Pressing mute mid-conversation also cancels whatever the assistant was doing |

## Everyday things

- **Updates**: when a new EchoMuse release is out, the dashboard shows an
  update badge — one click updates the device over WiFi. The release notes
  appear alongside it, so you can read what changed before deciding, rather
  than judging by version number. If an update ever
  goes wrong, the device automatically rolls back to its previous version.
  **Deploy all** updates the whole fleet at once; it runs in the background,
  so you can close the dialog and reopen it from the header pill to check
  progress (the button itself steps aside until the fleet is done).
- **Settings**: everything tunable lives in the dashboard, either fleet-wide
  (the gear icon) or per device. See [configuration.md](configuration.md).
- **Terminal**: each device page has a full remote terminal (for the
  curious; you never *need* it).
- **Volume**: buttons on the Dot, the dashboard slider, or Home Assistant's
  media player card — they all stay in sync.
- **Interrupting**: with barge-in enabled, say the wake word while it's
  talking and it stops and listens. The mute button also cuts it off
  instantly (and mutes).
- **Bluetooth proxy** (optional): each Dot can double as a Home Assistant
  Bluetooth proxy — passively picking up BLE advertisements (presence
  beacons, BLE sensors) and feeding them to HA as a *separate* ESPHome
  device, independent of the voice assistant. Enable it per device in the
  Config tab (Bluetooth section); it appears in HA as "<name> BT Proxy". See
  [configuration.md](configuration.md).

## When something doesn't work

1. Is the device **online** in the dashboard?
2. Does the wake word register? The Activity tab shows recent wake detections
   and "near-misses" (times it almost triggered) — if you're getting
   near-misses, nudge the sensitivity up a step (see configuration.md).
3. Bad transcriptions? See the microphone section of
   [voice-pipeline.md](voice-pipeline.md) — room noise and speaker distance
   are the usual suspects. To stop guessing, turn on **Save utterances**
   (Config → Microphones → Advanced) and *listen* to what the Dot heard —
   the Activity tab gains a play button on each turn. It's off by default
   because it stores speech on your server; see
   [configuration.md](configuration.md) for exactly what's kept.
4. The troubleshooting section of [SETUP.md](../SETUP.md) covers the deeper
   stuff.
5. Still stuck, and want to ask? The dashboard's **Support** tab downloads a
   single diagnostic file to attach to a GitHub issue — versions, device
   state, recent logs and the delivery statistics that make audio problems
   diagnosable at a distance. It is built as an allowlist: no transcripts, no
   recordings, no network names, no account names, and device labels are
   replaced with pseudonyms. [support-bundle.md](support-bundle.md) lists
   exactly what's in one so you can check before you share it.
