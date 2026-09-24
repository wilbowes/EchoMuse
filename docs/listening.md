# Listening: who decides, and when audio leaves the Echo

This is the specification for how an Echo listens for its wake word and when
its microphone audio leaves the device. It is the reference for the firmware
(`device/internal/listen/`), the controller (`controller/em_listen.py`), the
wire protocol (`docs/device-controller-interface.md`) and every user-facing
privacy statement. If code and this document disagree, one of them is a bug.

## Two modes, chosen per Echo

| Mode | Stored value (`owwOnDevice`) | Wake word runs on | Audio leaves the Echo |
|------|------------------------------|-------------------|-----------------------|
| **On this Echo** (default) | `on` | the Echo | only while you are talking to it |
| **On the controller** | `off` | the controller | **all the time**, to the controller on your LAN |

There is no third user-facing mode. `shadow` (both detectors scoring the same
continuous stream, for comparing them) still exists as a **developer
diagnostic**: it is not offered in the dashboard and is set through the config
API. An Echo already in it shows it, labelled as streaming, because it does.

Each Echo is in exactly one mode. A fleet can mix them, and arbitration works
across the mix (see *Arbitration*). The dashboard shows each Echo's mode, says
plainly when an Echo streams, and summarises the fleet ("1 of 3 Echoes streams
audio continuously").

The stored values are kept from the three-mode design (`off`/`shadow`/`on`) so
existing configuration and firmware keep their meaning; only the presentation
changed.

## What "On this Echo" means, exactly

**No audio leaves the Echo until its own wake word detector fires.** Then it
streams the audio that follows the wake word, until any one of:

1. the controller says the utterance has ended (Home Assistant's end-of-speech,
   or the controller's own endpoint when HA's never engages),
2. the controller declines the wake (another Echo won arbitration, no Home
   Assistant connection, a turn already running with barge-in off),
3. the controller has not acknowledged the wake within `ackTimeout` (3s),
4. the session has been open for `maxOpen` (30s),
5. the Echo is muted, or its data connection drops.

Rules 3–5 are enforced **on the Echo**, so a lost message, a crashed controller
or a network partition cannot leave it streaming.

While a reply plays, the Echo keeps listening **locally**. Saying the wake word
over the reply (barge-in) is detected on the Echo, which then opens a new
session exactly as above. Nothing streams during the reply unless the wake word
is heard.

Only two things open the microphone without a local wake, and both are the
user asking, or being asked, to speak: the **action button**, and a Home
Assistant **follow-up question** ("continue conversation", or an
`ask_question` / `start_conversation` action). Both use the bounded turn stream (`mic_start` with
`lock_mic:true`), which the Echo gates on speech and ends itself after 5s of
nothing; the controller ends it at end of speech exactly as it closes a
session, and the Echo goes back to listening locally.

### What is honest to claim, and what is not

- Say: *audio leaves the Echo only after it hears the wake word, and only
  until you finish speaking.*
- Say: *a false wake sends a few seconds of audio you did not intend.* That is
  true of every wake word system, Amazon's included, and we publish it rather
  than be caught by it.
- Do not say *audio never leaves the device* or *fully local*: the command goes
  to the controller and on to Home Assistant's speech-to-text, wherever the
  user configured that to run.

## States an Echo can be in

The controller resolves one of these per Echo (`em_listen.resolve`) and the
dashboard shows it. Configuration says what was asked for; this says what is
true.

| State | Meaning | Streams continuously? |
|-------|---------|-----------------------|
| `local` | On-Echo wake word, listening | no |
| `controller` | Controller wake word, by configuration | **yes** |
| `diagnostic` | `shadow`, both detectors on | **yes** |
| `legacy` | Asked for On this Echo, but the firmware predates `oww_local_only` | **yes** — says "update firmware for private listening" |
| `degraded` | Asked for On this Echo, but the Echo cannot score (runtime or model missing, or failed to load) | no — **button only**, and says why |
| `unknown` | The Echo has not reported yet | shown as unknown, never as private |

**`degraded` never falls back to streaming.** A device that cannot run its
own wake word is either broken or waiting on an install, and it still answers
the button. Quietly streaming instead would make the dashboard's privacy
statement false without anyone choosing it.

`legacy` does stream, and is shown doing so. That is today's firmware
behaviour; the fix is a firmware update, and the dashboard says so.

The state comes from the Echo itself (`listen_state` on `/control`), never from
the controller's reading of configuration: the Echo is the only party that
knows whether its scorer loaded. `oww_local_only` in `capabilities` says the
firmware *can*; `listen_state` says whether it *is*.

## The session protocol

All of this is negotiated: the Echo announces `oww_local_only`, the controller
announces `listen_session` on its `ack`. **Local listening engages only when
both are present.** Against an older controller the Echo keeps its current
behaviour (continuous stream, triggering on its own wake), because an old
controller only acts on a device wake when frames are arriving.

### Messages

Echo → controller, `/control`:

| `type` | Fields | Meaning |
|--------|--------|---------|
| `listen_state` | `state` (`local`/`stream`/`degraded`), `reason?` | Sent on every change and after every `ack` |
| `oww_wake` | `score`, `threshold`, `ageMs`, **`session`**, **`floor`**, **`barge`** | A local wake opened session `session`. `ageMs` is how long ago the wake word's last frame was **captured**. `floor` is the Echo's noise floor (RMS). `barge` is true when the speaker was playing |
| `listen_end` | `session`, `reason` | The Echo closed a session on its own (`ack_timeout`, `max_open`, `muted`, `link`) |

Controller → Echo, `/control`:

| `type` | Fields | Meaning |
|--------|--------|---------|
| `listen_ack` | `session` | The wake was taken; stops the `ackTimeout` clock |
| `listen_close` | `session`, `reason` | End the session. **Ignored if `session` is not the open one** |

`mic_stop` changes meaning slightly under private listening: it ends a bounded
turn stream but never a session (those end only by id) and never the Echo's
local listening, which is what hears a barge-in over the reply. `mic_start` with `lock_mic:true` replaces the
local wake stream for the length of the turn.

Echo → controller, `/data`:

| Code | Layout | Meaning |
|------|--------|---------|
| `0x07` | `[0x07][session u32 BE][seq u16 BE][PCM]` | Session audio, mono `S16_LE` 16 kHz |

### Why sessions are numbered

Control and data travel on different sockets, so a session's first audio can
arrive before or after its `oww_wake`, and a late frame of one session can
arrive after the controller has moved on. Tagging every frame with its session
makes both harmless: the controller holds frames for a session it has not yet
heard about (bounded, `PENDING_MAX_S`), delivers them the moment the wake
arrives, and drops frames for any session it has closed. Untagged audio was
routed by a flag, and a frame landing on the wrong side of a flag flip became
the first half-second of the *next* user's command.

### The first audio of a session

The Echo keeps a ring of recent processed audio (`ringMs`, 2s) with the
capture time of every 80 ms frame. A wake reports the capture time of the
frame that crossed; the session starts with every ringed frame captured
**after** it, then continues live. The controller's existing
`VOICE_PREROLL_DISCARD` removes the wake word's tail, exactly as it does for a
controller-detected wake.

Timestamps come from one `time.Now()` taken when the frame is handed to both
the scorer and the ring, so the scorer's queue delay (up to 640 ms when busy)
never shifts where the session starts, and `ageMs` measures from capture rather
than from when inference finished.

## Wake thresholds on the Echo

The Echo scores against:

- `owwThreshold` normally;
- `bargeInThreshold` while its speaker plays a response or an alarm, **or**
  music, when barge-in is enabled — the same rule the controller applies;
- and while that lower bar is in force, it needs **two consecutive frames**
  above it (`em_barge.decide`'s rule), because a single frame at a bar ten
  times below the wake threshold fired on the assistant's own voice.

With barge-in off, a wake heard while a reply plays is still reported and the
controller declines it (`listen_close`), so the rule lives in one place.

## Arbitration

First to **hear** wins, not first to arrive. Each claim carries the time its
audio was **captured**, in the controller's clock, measured so that time spent
in flight cannot move it. A home WiFi link loses packets, TCP retransmits
them, and a message can arrive seconds late; arrival time is exactly the
number that lies.

- **A wake the controller scored** is dated by the frame that crossed. The
  continuous stream sends every 80 ms frame, silence included, so frame *n*
  was captured *n* × 80 ms after the stream began; the controller learns
  when that was from the frames that arrived with the least delay
  (`em_listen.CaptureClock`, a sliding minimum that follows the Echo's clock
  drifting against ours). A frame held a second in a retransmit is still
  dated to its capture, and so is one that waited in the controller's own
  queue before it was scored.
- **A wake the Echo detected** carries `capturedMono`, the capture instant
  on the Echo's monotonic clock. The controller maps that clock onto its own
  from the ping replies it already exchanges every 5 s, each of which
  carries the Echo's `mono`: the reply with the shortest round trip in the
  last two minutes pins the mapping to within half that round trip
  (`em_listen.DeviceClock`, the rule NTP uses). A retransmit only lengthens a
  round trip, so it is never the one chosen. Firmware that sends no
  `capturedMono` falls back to arrival − `ageMs` − half the smoothed RTT,
  which is right only when the message was not delayed.

What remains uncorrected is the least delay any frame or ping had — a few
milliseconds — and the Echo's send batching, at most one 80 ms frame; both
are well inside `wakeArbitrationMs`. Neither estimate is ever later than
arrival or more than 3 s before it.

A claim cedes if it was heard within `wakeArbitrationMs` of the current
winner's, whenever it arrives. A granted claim is **never revoked** — that
would cut off a turn already listening — so the winner is the first claim to
arrive among those heard within the window, and the winner is held for
`wakeArbitrationMs` plus 3 s so a late claim still finds it. Holding costs
nothing: a separate wake in another room is told apart by when it was heard,
not by when it arrived. 3 s is the Echo's ack timeout; a private wake later
than that has already closed its session, and the controller ignores a wake
for a session the Echo has closed.

**A mixed fleet waits; a uniform one does not.** When some Echoes detect the
wake word themselves and others are scored by the controller, the two paths
reach the arbiter at different speeds, so the first claim to arrive is not
the first heard: on 2026-09-24 an Echo 10 m away, detecting on the device,
arrived 16 ms ahead of one a metre from the speaker that the controller was
scoring, and took the turn. So on a mixed fleet the first claim is held until
250 ms after it was heard (`MIXED_HOLD_S`, less whatever it already spent in
flight), every claim heard within the window by then is collected, and the
one heard **earliest** wins. Nothing is revoked: no one holds the turn until
the hold ends. A fleet that detects one way races on equal terms and grants
the first arrival at once, as above. An Echo whose mode is not yet known
counts as different, so the fleet holds rather than guesses. A barge-in
during playback fires on the second of two frames and is dated from the
first.

The wake log line reports, for a controller-scored wake, how long after
arrival it was scored and how long its frame spent in transit.

## What each mode costs and loses

| | On this Echo | On the controller |
|--|--------------|-------------------|
| Echo CPU | ~0.4 of a core, always | none for wake word |
| WiFi | nothing while idle | ~32 KB/s per Echo, always |
| Near-miss counter on the dashboard | `—`: the Echo's own window stats carry its peak score | counted on the controller |
| Controller comparison score (`ctrl_wake_score`) | NULL — nothing to compare, and not logged as a miss | recorded |
| Survives a controller restart | wake still detected, no one to answer | no |

Absent measurements store as NULL, never 0: an Echo that did not stream has no
controller score, which is not a controller that scored zero.
