# Configuration Guide

Every setting, what it actually does, and when you'd touch it — in plain
language.

## Where settings live

- **Fleet config** (gear icon → Fleet Config): the defaults every device
  uses.
- **Per-device config** (device page → Config tab): flip the override toggle
  and that device gets its own copy of the settings, ignoring fleet changes
  until you flip it back.

Changes apply **immediately** — no restarts, no rebuilds. The Config tab
opens with the device's **network (WiFi)** settings at the top — always
per-device, never inherited from the fleet — followed by the
fleet-inheritable sections, in order of how often you'll realistically touch
them: **Playback**, **Wake word**, **Microphones**, **Ring**, **Advanced**,
**Bluetooth**.

Two other device tabs worth knowing: **Status** (IP, firmware, WiFi network,
ESPHome port, resource meters, and the Bluetooth-proxy diagnostics panel when
enabled) and **Activity** (voice-turn history — what was heard, how it was
transcribed, wake-word scores, playback underruns, near-misses). Activity
history is stored in the controller's database, so it survives controller
and device restarts; hourly hardware trends (CPU, memory, WiFi signal) are
kept for 180 days and available via the API
(`/api/devices/{id}/activity?days=N`).

---

## 01 — Playback

How responses sound.

### Equalizer (8 faders + presets)
Shapes the tone of the voice responses, like the EQ on a stereo. The Dot's
little speaker is boomy and dull by default.

- **Flat** — no shaping.
- **Clarity** — boosts the upper-mid frequencies where speech intelligibility
  lives. Good default for voice.
- **Warmth** — gentle low-mid lift, softer top. Nicer for music-ish content.
- Drag any fader for a custom curve.

### Speech boost
An extra presence bump for spoken responses. Try it if responses sound
muffled from across the room.

### Startup volume
The volume the device wakes up with after a reboot or power cut — so a
midnight power blip doesn't come back at full blast. Day-to-day volume is
whatever you set with the buttons/HA; this is just the reset point.

---

## 02 — Wake word

How the device decides you said the magic word. (This work actually happens
on the controller, not the Dot — the Dot just streams audio to it.)

### Wake word model
Which word wakes it: Hey Jarvis, Alexa, Hey Mycroft, or Hey Rhasspy. These
are pre-trained recognisers — you're picking a word, not training anything.
Pick one that doesn't collide with words you say a lot (and if your
household still talks to real Alexas, don't pick Alexa).

### Sensitivity (Precise ↔ Eager)
The confidence bar the recogniser must clear.

- Toward **Precise**: fewer false wakes (it triggering off the TV), but it
  may ignore you sometimes.
- Toward **Eager**: catches you more reliably, but expect the occasional
  ghost activation.

**How to tune it**: the Status tab counts **near-misses** — moments where the
score came close but didn't trigger. If you're being ignored and see
near-misses climbing, move one step toward Eager. If it wakes up when nobody
spoke, move toward Precise.

### Barge-in
Lets the wake word **interrupt the assistant mid-turn** — say "Hey
Rhasspy, stop" while it's reading you a paragraph (or still thinking
about your last question) and it cuts off and listens. Off by default. **Turn on Echo cancel (AEC) first**: barge-in
works by leaving the microphones live while the device speaks, and AEC is
what stops it hearing itself. The **barge threshold** is the wake
confidence required during playback — and counter-intuitively it should be
much *lower* than the normal wake threshold (≈0.10 works well): the
speaker is far louder at the microphones than you are, so your voice
scores lower over playback than in a quiet room, while the device's own
(echo-cancelled) voice barely scores at all (0.002–0.003 measured since
v2.7.8). **0.05 is a good default** — you shouldn't need to raise your
voice much. Raise it if responses ever cut themselves off. (During the
silent *thinking* pause the normal wake sensitivity applies instead —
nothing is playing, so the low barge threshold isn't needed there.)

### Speex denoise
Runs a noise cleaner on the audio *only for wake-word scoring* (your actual
commands are untouched). Worth trying in rooms with constant background
noise (TV, air-con) if wake detection is unreliable there. Off by default —
it's a "try it and compare" option.

---

## 03 — Microphones

How your voice gets captured. These settings were tuned carefully — the
presets are the only part most people should touch.

### Pickup presets (Omni / Front / Rear)
The Dot has 7 microphones. During a command, it can favour the mic closest
to your voice:

- **Omni** — use the centre mic for everything. The safe choice; also the
  fallback if directional pickup ever misbehaves.
- **Front / Rear** — permanently favour one side. For Dots against a wall or
  next to a TV: point the pickup *away* from the noise.
- With directional pickup on and no fixed direction, the device picks the
  mic automatically at each wake — see the pipeline doc's "lock-back"
  section.

### Advanced (inside the Microphones section)

**MICPGA / Digital gain** — hardware amplifier levels inside the Dot's audio
chips, matched to Amazon's own factory values. *Leave these alone* unless
you're deep-diving; wrong values can distort every mic at once.

**Mic gain (dB)** — the software gain applied to the raw 24-bit microphone
signal before anything else hears it. Default **24dB**, chosen from real
measurements (the Dot's raw capture is extremely quiet — without this boost,
speech recognition regularly failed). Raise only if a device in a very large
room still tests quiet; the device reports "clipped" samples in its log if
you've gone too far. Lower toward 0 if you ever see clipping.

**Beam angle / Beamforming** — the raw controls behind the pickup presets.
Beam angle `-1` means "choose automatically at each wake"; any other number
fixes the pickup direction in degrees (0 = the side with the volume-up
button, clockwise). The presets set both of these for you.

**Noise suppression** — cleans the audio sent to speech-to-text (and only
that — wake-word listening is untouched). It uses a small neural denoiser
(DTLN) running on the controller, so there's no load on the Dot. Helps most
with *steady* noise — fans, air-con, appliance hum — in rooms where
transcripts come back garbled. It does not remove other people talking or
the TV; pointing the beamformer away from them is the tool for that. Off by
default — turn it on per device and compare transcripts.

**Echo cancel (AEC)** — teaches the mics to *subtract the Dot's own voice*
from what they hear. Benefits: the device can hear you properly during and
right after its own responses (follow-up questions work much better), its
own speech can't confuse the listening logic, and it's what makes barge-in
possible. Off by default; turn it on per device and check the `[aec] att=`
lines in the device log show attenuation climbing during a response. Two
tuning knobs:

- **AEC delay** — alignment between what was played and what the mics
  heard. **Leave it at 0** — that's the measured correct value for this
  hardware (the mic pipeline's own buffering absorbs the speaker latency).
  Raising it can silently disable cancellation entirely.
- **AEC tail** — how much room echo/reverberation the canceller models.
  Default 300ms; raise toward 500 in big empty-sounding rooms.

---

## 04 — Ring

The colours the LED ring uses during conversations. Scenes apply
instantly and can differ per device. On current firmware (v2.9+) the
device animates the ring itself — the controller sends one "play this
animation" instruction per state change, so the spinner stays perfectly
smooth regardless of WiFi or controller load, and while a response is
speaking the ring **throbs in time with the audio** (brightness follows
the actual level coming out of the speaker). If the controller ever
vanishes mid-conversation the ring times itself out rather than spinning
forever. Older firmware falls back to controller-rendered frames.

- **Standard** — the classic green.
- **Airy** — a pale, calm sky blue.
- **Malevolent** — deep crimson listening ring with an ember spinner.
- **Pride** — a rotating rainbow.
- **Custom** — pick your own **Listening** (solid ring while recording) and
  **Thinking** (spinner while processing) colours.

Two things never change, in every scene: the **red mute ring** (red always
means the microphones are off — it's a privacy indicator, not decoration)
and the cyan volume arc. The directional "which mic is listening" highlight
also adapts automatically: it brightens the scene's ring colour rather than
painting green.

## 05 — Advanced

Everything in this section affects **only button-press conversations**
(holding the action button to talk without a wake word). Wake-word
conversations ignore all of it — they're managed by Home Assistant's own
speech detection.

### Turn processing

**Noise suppression** — an AI noise remover (RNNoise) on button-turn audio.
Currently kept **off**: the version in the firmware expects a different
audio format and does more harm than good until that's fixed. The toggle
exists so the fix can be tested without a firmware update.

**Auto gain (AGC)** — automatically levels your voice volume on button
turns, so whispering and shouting come out similar. Harmless here; it's
deliberately never applied to wake-word listening (automatic gain drifting
with room noise was the root cause of a "stops responding after a few days"
bug, and it stays banished from that path).

### Speech gate

Decides when a button-press utterance starts and stops:

- **Threshold** — how loud counts as "speech". Measured in pre-gain units
  (the mic gain doesn't change what this number means). The default 0.001
  was validated by measurement; raise slightly (0.003–0.005) only in
  genuinely noisy rooms.
- **Speech gate (ms)** — how much continuous speech opens the gate. Higher =
  ignores brief noises, but clips fast talkers.
- **Silence gate (ms)** — how much silence ends your turn. Higher = you can
  pause mid-sentence without being cut off; lower = snappier responses. 900ms
  default; raise to ~1200 if you get cut off mid-thought.

---

## 06 — Bluetooth

**Bluetooth proxy** — turns the Dot into a Home Assistant Bluetooth proxy.
The device passively listens for Bluetooth Low Energy advertisements
(presence beacons, BLE temperature/humidity sensors, phones and watches for
room-presence systems like Bermuda) and forwards them to Home Assistant.

In Home Assistant the proxy appears as a **separate ESPHome device** (named
`<label> BT Proxy`), independent of the voice assistant — you can add,
remove, or ignore it without touching the voice satellite. Once added, its
scanner feeds HA's Bluetooth integration exactly like an ESP32 Bluetooth
proxy would, and a diagnostic sensor counts received advertisements.

Two things to know before enabling:

- Enabling **permanently switches the Dot's Bluetooth chip away from
  Android's stack** (it survives reboots). Nothing EchoMuse uses needs
  Android Bluetooth — but stock-style Bluetooth speaker pairing stops being
  possible on that device.
- The proxy is **receive-only** (passive scanning). Devices that need an
  active connection to read data (some smart locks, older BLE devices)
  aren't supported — advert-based sensors and presence tracking are.

Diagnostics live on the device's **Status tab** (Bluetooth proxy panel):
scanner state, advertisements seen, nearby device count, and whether Home
Assistant is connected and receiving.

---

## WiFi (device page → Config tab, top section)

Move a device to a different WiFi network without touching ADB. The section
at the top of the Config tab shows the current network, signal, and IP, lets
you scan for visible networks, and switches with a confirmation step.

The switch is designed to be **unbrickable**: the device applies the change
itself and must pass three checks — join the network, get an IP, and
**reconnect to this controller** — before the change is kept. Fail any of
them (wrong passphrase, DHCP trouble, or a network that works but can't
reach the controller, like an isolated guest VLAN) and it automatically
restores the previous network and tells you why. Even a power cut
mid-switch recovers: an unconfirmed change is rolled back on boot. Allow
about two minutes for the device to drop off and come back.

---

## Controller settings (the `.env` file)

These are set once, on the server, and need a controller restart to change:

| Setting | What it is |
|---|---|
| `SERVER_IP` | The controller computer's LAN IP — what devices connect to. |
| `OWW_MODEL` / `OWW_THRESHOLD` | Startup defaults for wake word/sensitivity — the dashboard values override these. |
| `DEVICE_APPROVAL` | `strict` (you approve every new device — recommended) or `auto`. |

See `.env.example` for the complete list with comments.
