# Configuration Guide

Every setting, what it actually does, and when you'd touch it — in plain
language.

## Where settings live

- **Fleet config** (gear icon → Fleet Config): the defaults every device
  uses.
- **Per-device config** (device page → Config tab): flip the override toggle
  and that device gets its own copy of the settings, ignoring fleet changes
  until you flip it back.

Changes apply **immediately** — no restarts, no rebuilds. The config page is
organised into four sections, in order of how often you'll realistically
touch them: **Playback**, **Wake word**, **Microphones**, **Advanced**.

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
Lets the wake word **interrupt the assistant mid-response** — say "Hey
Rhasspy, stop" while it's reading you a paragraph and it cuts off and
listens. Off by default. **Turn on Echo cancel (AEC) first**: barge-in
works by leaving the microphones live while the device speaks, and AEC is
what stops it hearing itself. The **barge threshold** is the extra
confidence required during playback (higher than normal, so the device's
own voice can't trigger it) — lower it if interrupting feels unreliable,
raise it if responses ever cut themselves off.

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

**Echo cancel (AEC)** — teaches the mics to *subtract the Dot's own voice*
from what they hear. Benefits: the device can hear you properly during and
right after its own responses (follow-up questions work much better), and
its own speech can't confuse the listening logic. Off by default; turn it on
per device and check it behaves. Two tuning knobs:

- **AEC delay** — how long sound takes to travel from "the software played
  it" to "the mics heard it" (mostly internal buffering). Default 250ms. If
  echo cancellation seems weak, try 300–350.
- **AEC tail** — how much room echo/reverberation the canceller models.
  Default 300ms; raise toward 500 in big empty-sounding rooms.

---

## 04 — Advanced

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

## Controller settings (the `.env` file)

These are set once, on the server, and need a controller restart to change:

| Setting | What it is |
|---|---|
| `SERVER_IP` | The controller computer's LAN IP — what devices connect to. |
| `VOICE_MODE` | `esphome` (Home Assistant, the supported mode) or `claracore` (legacy custom backend, being retired). |
| `OWW_MODEL` / `OWW_THRESHOLD` | Startup defaults for wake word/sensitivity — the dashboard values override these. |
| `DEVICE_APPROVAL` | `strict` (you approve every new device — recommended) or `auto`. |

See `.env.example` for the complete list with comments.
