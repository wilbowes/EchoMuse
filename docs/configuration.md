# Configuration Guide

Every setting, what it actually does, and when you'd touch it — in plain
language.

## Where settings live

- **Fleet config** (gear icon → Fleet Config): the defaults every device
  uses.
- **Per-device config** (device page → Config tab): each section carries its
  own **Fleet / Device** switch in its header.

Scoping is **per section**, not all-or-nothing. Leave a section on *Fleet*
and it keeps following the fleet-wide value, including future changes. Flip
it to *Device* and only that section becomes this device's own — everything
else carries on tracking the fleet.

So a Dot in a small room can have its own **Ring** scene and its own
**Microphones** gain while still picking up every fleet change to the wake
word, EQ and Bluetooth settings. Before this, one override forked *all* the
settings and froze them against fleet changes permanently.

A section showing *Fleet* is displayed read-only rather than hidden, so you
can always see what it is inheriting. The banner at the top of the tab
summarises — `Fleet`, or `Local override (2 of 6)` with the sections named —
and **Revert all to fleet** puts everything back.

Flipping a section back to *Fleet* **discards** the values it was holding.
There is no hidden shadow copy waiting to reappear if you flip it to *Device*
again months later; it starts from the fleet value.

Changes apply **immediately** — no restarts, no rebuilds. The Config tab
opens with the device's **network (WiFi)** settings at the top — always
per-device, never inherited from the fleet — followed by the
fleet-inheritable sections, in order of how often you'll realistically touch
them: **Playback**, **Wake word**, **Microphones**, **Ring**, **Advanced**,
**Bluetooth**.

The **CPU** meter shows the core count beside the percentage — "27% · 2/4
cores". The Dot has four CPU cores and parks the ones it isn't using, and the
percentage is a share of the cores that are *awake*, so the same amount of
work reads as a bigger number when fewer are. Without the core count beside
it the figure can appear to halve when nothing actually changed.

Two other device tabs worth knowing: **Status** (IP, firmware, WiFi network,
ESPHome port, current volume, whether the config is fleet or overridden,
resource meters including **Latency** (the round trip to the device — amber
past 200ms, red past 1s; the only link-health signal the Echo's WiFi driver
actually provides, since it reports no retry or noise figures) and **Temp**
(the Dot's CPU sensor, and the hottest of its eleven sensors when that's
meaningfully warmer; it idles around 33°C, so anything amber is genuinely
unusual — and if the chip's thermal governor ever starts capping CPU
capacity, this is where it says so), and the
Bluetooth-proxy diagnostics panel when enabled —
the Status row reads `Online`, or `Offline` with how long ago the device was
last heard from) and **Activity** (voice-turn history — what was heard, how it was
transcribed, wake-word scores, playback underruns, near-misses, and — if
**Save utterances** is on — the recorded audio of each turn, playable and
downloadable). Activity
history is stored in the controller's database, so it survives controller
and device restarts; hourly hardware trends (CPU, memory, WiFi signal) are
kept for 180 days and available via the API
(`/api/devices/{id}/activity?days=N`).

---

## 01 — Playback

How responses sound.

**Two things to know before you tune anything here.**

**Changes take about four seconds to be heard.** Audio is sent to the device
several seconds ahead of when you hear it, so what is playing right now was
processed before you moved the control. Wait five seconds before judging, and
do not flip a setting back and forth quickly — you will be listening to the
old audio and conclude nothing happened.

**Change the settings on the device you are listening to.** If a device has
its own Playback settings (the Fleet/Device switch on that section), editing
the fleet defaults will not affect it. The save succeeds either way, so the
symptom is a control that appears to do nothing at all.

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

### Bass guard
Drops bass the speaker physically cannot produce.

It sounds backwards, and it is the biggest single improvement available for a
speaker this small. Frequencies below about 115Hz still move the cone even
though you cannot hear them, and that movement muddies everything above it —
which is what people usually describe as sounding thin, boxy or "tin-can".
Taking them away makes the middle clearer, and slightly *louder*, because the
limiter no longer has to hold everything down to fit bass peaks you were never
going to hear.

Quiet passages keep their low end. Only loud content is affected, which is why
this is a guard rather than a filter.

**Depth** sets how far it pulls down, default **−30dB**.

**Judge this by turning it off and on, not by moving the depth.** Almost all
of the audible change happens in the first few dB: switching it on takes about
5dB off the overall level and lifts the midrange, while going from −20 to −40
changes the overall level by **0.14dB** — below what anyone can hear. If you
A/B two depths and cannot tell them apart, nothing is broken; there is
genuinely almost nothing there to hear.

**Set the equalizer flat before you compare.** With the bands boosted hard the
limiter is already pulling the level down, so removing bass just lets it pull
down less — the loudness ends up the same and the guard sounds like it is
doing nothing. Measured: at a flat EQ, turning the guard on is a 7.7dB change
in overall level; with every band at +12dB it is 0.2dB, and the difference
shows up as 4dB more midrange instead. Both are the guard working.

Turn the whole thing off if you would rather — nothing else depends on it.

The frequency and the shape of the curve come from measurements of Amazon's
own firmware on this same speaker, so they are not guesses. The default depth
is gentler than Amazon's, because they pair theirs with an equalizer curve we
have not measured.

### Limiter
Stops the equalizer distorting what it boosts, and should be left on.

Turning any EQ band up can push the audio past the maximum the hardware can
represent. Without a limiter that gets clipped — measured at nearly 5% of
samples on an ordinary response with a modest bass boost — which is audible as
harshness or crackle, and it only ever happened to people who had touched the
EQ to make things sound better.

**Ceiling** is the peak level the output is held below, default −1dB. **Release**
is how quickly it recovers, default 150ms: shorter sounds louder, longer sounds
smoother. Neither needs changing unless you want to.

### Duck depth
How far music drops while the assistant is talking over it. Music **keeps
playing** through a voice turn — it isn't paused — so the answer arrives
mixed over a quiet bed, and the bed comes back up when the answer ends.

Default **−18dB**. Less negative (−6, −10) leaves the music more present;
more negative (−25, −30) all but silences it for the length of the answer.
It's worth setting by ear in the room it plays in: the right depth depends
on what you listen to and how loud, and there is no value that is correct
everywhere. The response itself is never turned down, only the music under
it — so if an answer is hard to hear over music, this is the setting, not
the volume.

Requires firmware **v2.10.0 or newer**, which mixes the two audio streams on
the device itself. That is not an arbitrary requirement: the controller runs
about four seconds ahead of what you actually hear, so when you say the wake
word those four seconds of music are already sitting on the Echo, past
anything the controller could still change. Older firmware shows the slider
disabled and falls back to pausing the music for the turn and resuming
after.

### Volume
Volume **tracks what you actually use** and survives reboots: every change —
buttons, Home Assistant slider, wherever — is remembered by the controller
and restored when the device reconnects. Set it low in the evening and a
midnight power blip brings it back low.

There is no volume slider in this section. There used to be, and it was
misleading: the device only re-applies the stored level on the first config
push after it boots, so moving the slider did nothing until the device
restarted — and any real volume change overwrote it in the meantime. Volume
is remembered device state rather than a setting you dial in, so the current
level is now shown read-only on the **Status** tab. Change it from Home
Assistant or the device buttons.

It is also never inherited from the fleet, whatever the section's Fleet /
Device switch says — otherwise a device would come back at another room's
volume.

**The top of the range changed in 2.20.0.** EchoMuse used to drive the
codec's digital volume past the point where it can only clip — measured at
65% distortion three button presses above the midpoint, and 89% at the
maximum, with the output no longer getting any louder. Stock Alexa never
touches that control, which is why EchoMuse sounded worse than stock when
turned up. The range now stops at the codec's unity gain, so the loudest
setting is quieter than it was and everything below it is cleaner.

The percentage Home Assistant shows also moved: a device at the same
physical level reads a higher number than before, because the scale no
longer includes a stretch that only distorted. Nothing changed about how
loud it actually is. If you have an automation with a volume threshold in
it, check that threshold.

The physical buttons step about 4dB per press across the audible range,
rather than spending presses near the bottom of a scale where nothing is
audible — silencing the device is the mute button's job. The cyan ring
spans that same range, so a press always moves it.

Mute is remembered too, but by the device itself: a muted Dot stays muted
through reboots, power cuts, and firmware updates — red ring and all —
whether or not the controller is reachable.

---

## 02 — Wake word

How the device decides you said the magic word. By default this work happens
on the controller, not the Dot — the Dot just streams audio to it. The Dot
can also do this work itself, either alongside the controller as a
comparison or instead of it; see **Wake word detection** below.

### Wake word model
Which word wakes it: Hey Jarvis, Alexa, Hey Mycroft, or Hey Rhasspy. These
are pre-trained recognisers — you're picking a word, not training anything.
Pick one that doesn't collide with words you say a lot (and if your
household still talks to real Alexas, don't pick Alexa).

Want your own word? Train a model with `oww_forge/` (see its README), then
use the **+ Custom model** tile to upload the `.onnx` — it's stored in the
controller's data volume, appears as a tile next to the stock words, and
takes effect immediately on selection. The `×` on an unselected custom tile
deletes it.

**If the device does its own wake word detection** (see *On-device wake
word*), the model has to be copied onto it before it can listen for the new
word. That happens automatically when you select it, and the device carries on
answering to its **current** wake word until the new one has arrived — usually
a few seconds. If the copy fails, the device stays on the word it already has
and the device log says why. It is never left listening for a word it does not
have.

**Changing the wake word briefly reconnects the device in Home Assistant.**
Home Assistant only reads a satellite's wake word configuration when it
connects, so the controller drops and remakes that connection to make the new
name show up. It takes a few milliseconds, but during it **every entity for
that device goes unavailable and comes straight back** — the voice assistant,
the media player, the action button, the ambient light sensor.

That matters if you have an automation using a **state trigger** on any of
them: coming back online is a state change, and the automation will fire. The
action button's event entity is the one people hit, because a returning event
entity restores its last event and looks exactly like the button being pressed
again. Exclude the transition:

```yaml
trigger:
  - platform: state
    entity_id: event.your_device_action_button
    not_from:
      - unavailable
      - unknown
```

Nothing is wrong with the device when this happens, and it only happens when
you change the wake word.

### Arbitration window
With more than one Echo, saying the wake word in earshot of two of them
used to start two competing conversations. Now the **first device to hear
you answers immediately**, and any other device detecting the same word
within this window (default 700ms) quietly stands down.

There is **no latency cost**: the winner claims the turn on the spot rather
than waiting out the window, so a solo wake is exactly as fast as it was
before. The window only decides how long afterwards a second device counts
as "the same utterance". `0` disables it, and it never applies when only
one device is online.

An earlier version instead waited out the window and gave the turn to
whichever device heard you *best*. That was dropped: it taxed every wake by
~364ms even when nothing was competing, and field data showed the
signal-to-noise winner produced a *worse* transcript than the device that
simply heard you first.

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

**What happens after you interrupt.** The device stops talking and listens
straight away — say the wake word and your new command in one breath and it
hears both, without waiting for a second prompt. This only started working
properly in 2.20.1: before that the interrupt cut the response off but the
command that followed was never picked up, so you had to wait and ask again.

**Interrupting cannot be taken back.** If you say the wake word and then stay
quiet, the original answer is gone rather than resumed — Home Assistant has no
way to restart a reply it has already abandoned. The turn just ends quietly.

### Speex denoise
Runs a noise cleaner on the audio *only for wake-word scoring* (your actual
commands are untouched). Worth trying in rooms with constant background
noise (TV, air-con) if wake detection is unreliable there. Off by default —
it's a "try it and compare" option.

### Wake word detection
Who decides you said the wake word. Three settings:

- **Controller** (default) — the Dot streams audio and the controller
  listens. What EchoMuse has always done.
- **Both (compare)** — the Echo *also* runs the same model over the same
  audio and reports what it would have detected, without acting on it. It
  never triggers a turn. This is the one to use first: it tells you whether
  on-device detection is trustworthy on your hardware, in your room, before
  anything depends on it.
- **On device** — the Echo decides, and the controller starts the turn on
  its word.

**Why you might want "On device".** The wake decision stops crossing your
network, so it is not delayed by a bad moment on the link. On a marginal
connection that is the difference between a Dot that responds promptly and
one that lags unpredictably.

Be clear about what it does **not** do:

- **It does not reduce network traffic.** The audio still streams
  continuously, because the controller runs the rest of the turn.
- **It does not keep working without the controller.** The wake word is only
  the first step; the turn itself needs the controller for Home Assistant,
  the microphone stream and the spoken reply. A wake detected while the
  controller is down lights the ring and goes nowhere.

The controller keeps listening alongside it, which keeps the comparison in
**Activity** running so you can see whether the two agree. That costs nothing
*extra* — it is the same work the controller was already doing in
**Controller** mode — but it is work that is no longer strictly needed once
you trust the device, and on a busy Home Assistant machine you may prefer not
to pay for it. **Both (compare)** is the mode built for measuring; consider
dropping back to it when you want the numbers rather than leaving them
running forever.

Barge-in — interrupting a response by speaking over it — is scored by the
controller in every mode and is unaffected by this setting.

Each voice turn's row in **Activity** shows both scores side by side, and
the per-device activity API returns an agreement summary (how often they
agreed, how far apart in milliseconds, and crossings the device saw that
never became a turn).

**Multi-device caveat.** If you have several Echos in earshot of each other,
put only one on **On device** for now. The rule that stops two Dots
answering at once still judges claims by when they arrive rather than when
each Echo actually heard you, so a device whose message was delayed can lose
to one that heard you less well. With a single device set this way, or with
Echos that cannot hear each other, this does not apply.

Three things to know before leaving Controller:

- **It needs files installed on the Dot** that aren't part of the firmware —
  ONNX Runtime plus the wake-word models, about 15MB, placed in
  `/data/local/share/echomuse/oww`. They're deliberately not shipped in the
  firmware image, because that would double both the download and the space
  each of the two firmware slots takes. Until they're there, the setting does
  nothing and the device log says which file is missing.
- **It costs about half a CPU core, permanently**, because the wake stream is
  always on. Measured on an Echo Dot Gen 2 that has capacity for it — the mic
  pipeline was unaffected across hours of use, including during music
  playback — but enable it on **one device at a time** and watch the
  **Resources** panel on the Status tab.
- **It needs recent firmware**, and the two settings need different
  vintages: scoring shipped before triggering did. Each option is disabled
  and says so on an Echo whose firmware cannot do it, rather than appearing
  to work.

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

**Save utterances** — keeps the audio of recent voice turns so you can
*listen* to what was sent for transcription. The **Activity** tab then shows
a ▶ (play here) and a ⤓ (download the WAV) on every turn that has a
recording.

What's saved is the audio **exactly as speech-to-text received it** — so if
**Noise suppression** is on, you're hearing the cleaned-up version, not the
raw microphone. That's deliberate: when a transcript comes back wrong, the
only recording that can explain it is the one the recogniser actually heard.

This is the honest way to answer "is my microphone any good?". Without it
you're guessing from a garbled transcript, which can't tell you whether the
room was noisy, the gain was too low, or the denoiser chewed a word. Thirty
seconds of listening usually settles it — and it's the only sensible way to
A/B **Mic gain**, the pickup presets, or **Noise suppression**, since you can
compare the same phrase before and after.

**Off by default, and worth thinking about before switching on.** This is the
only setting that stores recognisable speech on the controller. What's kept:
the **last 10 turns per device**, as plain WAV files in the controller's data
folder, each overwritten as newer ones arrive. Only the audio sent for
recognition is saved — never the always-on wake-word listening, which is
discarded continuously and never written anywhere.

Turning the setting back off stops new recordings immediately, but **leaves
the ones already saved where they are** — deliberately, so that switching off
doesn't destroy samples you were part-way through comparing. They stay until
newer recordings push them out (which needs the setting back on) or you
delete the device, which removes its recordings too. To clear them out sooner,
delete the files from the controller's `data/recordings/` folder.

A turn recorded a while ago may show no buttons — that just means its
recording has aged past the last 10 and the turn history has outlived it.

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

The volume arc holds the ring for about two seconds so a turn animation
can't wipe it the instant it appears — but **pressing the action button
cancels it immediately**, so adjusting the volume and then talking to the
device still shows you the listening ring straight away.

### How a turn ends
The ring tells you *why* a conversation stopped, using rhythm rather than
colour (red, orange and cyan already mean mute, no-controller and volume):

- **One slow throb** — the device was listening and heard nothing.
- **A few quick blinks** — something went wrong (Home Assistant errored, or
  no speech came back).
- **Ring simply goes out** — normal end, or you cancelled it yourself.

The ring also now clears when the audio *actually* finishes, rather than
when the controller estimates it should have. On a slow WiFi link the old
estimate could clear the ring several seconds before the Dot had stopped
talking.

### Meter response (Advanced)
While a response plays the ring throbs with the live speaker level. The
**Advanced** panel here shapes how hard it throbs — the device renders it
locally, so changes apply on the next response with no restart:

- **Decay** — how fast it falls. Higher tracks individual syllables; lower
  reads as a slow swell.
- **Attack** — how fast it rises on a peak.
- **Gamma** — contrast. Higher makes the swing more visible.
- **Floor** — brightness during silence. `0` goes fully dark between words.
- **Reference** — the speaker level mapped to full brightness. Lower is more
  sensitive.
- **Curve** — below `1` lifts quiet consonants into view.

These are taste settings, which is exactly why they're adjustable here
rather than baked into firmware. The defaults are tuned for speech; if the
ring looks too static, raise **Decay** and **Gamma** first.

## 05 — Advanced

Everything in this section affects **only button-press conversations**
(tapping the action button to talk without a wake word — a *hold* is a
separate gesture that fires an event in Home Assistant instead). Wake-word
conversations ignore all of it — they're managed by Home Assistant's own
speech detection.

While the mic is muted a tap does nothing, but a hold still fires its event:
the mute silences speech, not the button.

### Make the tap an event instead

**Tap fires an event** (`buttonSingleTapEvent`) — turns a tap into a Home
Assistant event rather than the start of a conversation, so you can bind it to
anything you like. With it on, the device no longer starts a voice turn from
the button at all; the wake word is untouched, and a hold still fires `long`.
A tap fires while muted too, for the same reason a hold does — it's an event,
not speech.

Needs firmware v2.10.0 or newer. On older firmware the toggle is disabled and
says so.

**Bind destructive automations to the hold, not the tap.** The button has no
authentication, and a speaker sitting on a counter is a great deal easier to
tap by accident than to hold for three quarters of a second.

**Multi-tap window** (`buttonMultiTapMs`) — set it above zero and taps are
grouped into `single`, `double` or `triple`. The cost is that *every* tap is
delayed by this window, because a tap can only be called single once no
second one follows.

**Use 350ms.** Below about 300ms it fights both human timing — double taps
land roughly 150–400ms apart — and network jitter, because the gap is
currently measured when the taps reach the controller rather than on the
device. On a busy or distant device you may need more. Zero disables
grouping, and a tap fires `single` immediately.

### Turn processing

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

Note (v2.9.4): these two timings now behave exactly as configured. Older
firmware quietly applied them ~5× longer than the number said (a counting
bug against the mic's real batch size), so button-press turns used to hang
on for a few seconds of silence before ending — if turns feel snappier
after updating, that's why, and if a slow talker now gets clipped, raise
the silence gate.

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
| `SERVER_IP` | The controller computer's LAN IP — what devices are told to connect to. Leave it empty to detect it from this host; the controller refuses to start rather than advertise an address it had to guess at, and warns if the detected one looks like a container bridge. |
| `OWW_MODEL` / `OWW_THRESHOLD` | Startup defaults for wake word/sensitivity — the dashboard values override these. |
| `DEVICE_APPROVAL` | `strict` (you approve every new device — recommended) or `auto`. |
| `SERVER_TLS_PORT` | Encrypted device link (wss) port — default 8770, `0` disables. Devices switch to it automatically once they hold pushed credentials (wizard install, or the **Secure link** button on the device Status tab). |
| `REQUIRE_DEVICE_TLS` | Set to `1` **only after every device shows "wss (TLS)"** on its Status tab — from then on the controller rejects unencrypted or tokenless device connections. |

See `.env.example` for the complete list with comments.

### Encrypted device link

The controller generates its own certificate authority on first start
(stored in `tls/` next to the database) and listens for encrypted device
connections alongside the plain ones. Each device gets two credentials —
the CA certificate and a private token — installed automatically by the
provisioning wizard, or pushed to an existing device with the **Secure
link** button on its Status tab. A device with credentials connects
encrypted from its next reconnect; the Status tab's **Link** row shows
which mode each device is using. Once the whole fleet shows `wss (TLS)`,
set `REQUIRE_DEVICE_TLS=1` to lock out unencrypted connections entirely.

## What leaves your network

EchoMuse has **no telemetry**. There is no usage reporting, no analytics, no
crash reporting and no install counter. Nothing reports which features you
use, how many devices you have, or that you installed it at all. This is a
deliberate decision rather than an omission: the project exists to take a
cloud voice assistant off your network, and quietly adding a ping home would
undo the reason to run it.

A consequence worth stating plainly: **nobody, including the maintainers, can
tell how many people use EchoMuse.** Adoption is guessed at from GitHub stars
and release download counts, which is the trade being made.

### The one outbound connection

The controller contacts `api.github.com` once an hour to ask what the newest
release is, so the dashboard can tell you an update is available and show its
notes. When you choose to update a device, the firmware binary is downloaded
from `github.com` at that moment.

That is the whole of it. The request carries no identifiers — it is an
ordinary unauthenticated API call — but like any request it does reveal your
public IP to GitHub, the same exposure as a `git clone` or opening the repo
in a browser.

Set `update_check_interval` (seconds, default `3600`) in the system config to
change how often it runs. A long interval, say `86400`, reduces it to once a
day. Note that `0` does **not** disable checking — it currently makes the
poll loop spin without pausing, which is worse than leaving it alone.

### What never leaves

- **Voice audio and transcripts.** Mic audio goes from the device to your
  controller and on to your Home Assistant, over your LAN. What happens next
  is whatever your Assist pipeline does — if you have configured HA to use a
  cloud speech-to-text service, HA sends it there. EchoMuse itself sends it
  nowhere but HA.
- **Saved utterance recordings** (`saveUtterances`, off by default) — written
  to disk beside the database and never uploaded. Playing or downloading them
  is **admin-only**, as is seeing the transcript text of a turn: on the Home
  Assistant add-on every household user can reach the dashboard, so read-only
  accounts get turn timings, scores and outcomes without the speech. Enforced
  on the server, not just hidden in the page.
- **Device serials, WiFi credentials, network names and your fleet's
  configuration.** These live only in the controller's database.
- **Support bundles** are built only when you ask for one, and sharing the
  file is your decision. They deliberately exclude speech, transcripts,
  network names and account names — see
  [support-bundle.md](support-bundle.md).
