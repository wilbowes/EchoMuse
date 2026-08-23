# LED Ring State Model

The 12-LED ring (plus the discrete mute-button LED) is EchoMuse's entire user
interface. There is no screen and no other indicator, so the ring carries the
whole burden of telling someone what the device is doing.

**Core requirement:** the ring must be *unjarring*, *consistent*, and
*understandable*. A ring that lights and then leads nowhere is worse than a
ring that never lit.

This document is the authoritative behavioural spec: every ring owner, every
event, and the outcome for each. Entries are tagged:

- **[today]** — current shipped behaviour (v2.9.7)
- **[proposed]** — design not yet built, pending sign-off

---

## 1. Design principles

1. **Never show a state the system cannot honour.** Optimistic feedback is
   allowed only where it can be *bounded and resolved* — confirmed by the
   controller, or visibly withdrawn.
2. **Feedback local, decisions remote.** The device owns what it can know
   first-hand (a button was pressed, its own speaker buffer emptied, it is
   muted). The controller owns what requires knowledge the device lacks (was
   a wake word spoken, did HA answer, which device should respond).
3. **One meaning per signal.** Orange already means "controller unreachable".
   New failure modes resolve into existing vocabulary rather than inventing
   signals a user would have to learn.
4. **Every provisional state resolves.** No state may be held indefinitely on
   an assumption. Provisional paints carry a deadline; animations carry a TTL
   dead-man.
5. **Device-sovereign states are never overridden by the network.** Mute is
   the canonical case: it must be correct with the controller absent, wedged,
   or lying.

---

## 2. Ring owners — priority ladder

Highest layer that is active wins the physical paint. Lower layers still
*record* their state (`baseLEDs`) so the ring can be handed back correctly on
expiry.

| # | Owner | Visual | Lifetime | Sovereignty | Code |
|---|---|---|---|---|---|
| 1 | **Volume arc** | Cyan, N of 12 proportional | 2s window (`volumeLEDSecs`) | Device-local; physical presses only | `volume.go:145` |
| 2 | **Mute ring** | Solid red `(180,0,0)` + button LED (gpio444, active-high) | Until unmuted; survives reboot + OTA | **Device-sovereign**, persisted to `/data/local/etc/echomuse/state.json` | `mute.go:132` |
| 3 | **Link state** | Blue cylon sweep (connecting — hunting for a controller, #316) / orange sine pulse (disconnected — a full endpoint cycle failed) / white slow pulse (pending approval) | Until link resolves | Device-local | `cmd/server.go` `cylonSweep`/`pulseOrange`/`pulseWhite` (currently :1025/:1050) |
| 4 | **Turn / media animation** | `solid` · `spin` · `rotate` · `pulse` · `meter` · `off`, scene-coloured | Until replaced or `ttlSec` expires (30s listening / 135s spinner / per-response for the meter) | Controller-specified, device-rendered | `animator.go:53` |
| 5 | **Direction overlay** | Base ring colour brightened toward white at the beam angle | While listening ring is up | Device-local, requires `listeningLEDs` | `server.go:269` |
| 6 | **Idle** | All off | — | — | — |

### Suppression rules [today]

Both layer 1 and layer 2 suppress the hardware paint for layers 3–5, while
still recording into `baseLEDs`:

```go
if s.volume.DisplayActive() || s.mute.IsMuted() {
    return          // server.go:381 (SetLEDs), server.go:275 (SetDirectionLEDs)
}
```

Between layers 1 and 2, the volume arc wins for its window — both paint the
hardware directly, and the arc paints last. Its expiry timer is mute-aware and
restores the red ring rather than handing back to the controller:

```go
if vc.isMuted != nil && vc.isMuted() {
    lc.SetLEDs(redRing...)      // volume.go:177
} else if expire != nil {
    expire()                    // → paintBaseLEDs()
}
```

Layer 4 animations run on the device's own ticker and route every frame through
`SetLEDs`, so they inherit the suppressions and keep `baseLEDs` current. This
makes the volume-arc hand-back seamless mid-animation: the animator has been
updating `baseLEDs` throughout the suppressed window, so `paintBaseLEDs()`
paints the *latest* frame, and the next tick (≤80ms) resumes normally.

Replacement is atomic via a generation counter — a stale animation goroutine
can never paint over its successor (`animator.go:45-48`).

---

## 3. Link availability — three states, not two

Local feedback must be link-aware, which requires a sharper notion of
availability than the code currently has.

| State | Meaning | Local feedback permitted? |
|---|---|---|
| **LINKED** | Control WS registered *and* recent inbound evidence | Yes |
| **SUSPECT** | Socket believed up, but no recent evidence — or an interaction went unconfirmed | No — resolve to DOWN |
| **DOWN** | Socket closed, or awaiting approval | No — show link state (layer 3) |

### The problem with what exists [today]

```go
func (c *ControlClient) IsConnected() bool {
    return c.conn != nil        // control.go:131
}
```

`c.conn` goes nil only when the read loop errors, which is governed by
`wsPongWait = 45 * time.Second` (`data.go:61`). So for a silently-dead link —
controller stopped, WiFi blackhole, controller event loop wedged — the device
reports **connected for up to 45 seconds** after the controller is gone, and
the orange pulse does not start.

Gating optimistic paint on `IsConnected()` therefore produces a 45s window in
which the ring confidently lights and nothing happens. That window is *exactly*
when a user is most likely to be pressing the button — they press because
nothing responded. It satisfies the requirement literally and violates it
completely.

A second gap: socket liveness cannot see a controller that is connected but
**unable to serve** — HA down, pipeline erroring, event loop stalled.

### Passive detection has a floor [proposed]

Inbound evidence during idle arrives from the controller's ping loop every
**30s** (`em_controller.py:1674`). So a passive freshness threshold cannot be
tighter than ~35s without false positives. **Passive liveness alone cannot
give fast detection.** This is the structural reason the design below is
built on per-interaction confirmation rather than a connection flag.

### Per-interaction confirmation [proposed]

Don't ask "is the controller there?" Ask "did the controller acknowledge
*this* interaction?"

- On a local interaction, paint immediately and arm a **confirmation deadline**.
- Any controller message that owns the ring (`led_anim` / `leds`) before the
  deadline confirms it and replaces the provisional paint. **This mechanism
  already exists** — the generation counter does exactly this. No protocol
  change.
- Deadline expires with nothing → the interaction was not honoured. Transition
  to DOWN and show the orange pulse (layer 3).

Measured basis: the controller emits its response **sub-millisecond** after a
button event arrives, so the deadline needs to cover only two network hops.
Observed uplink latency, detection → first mic frame, over 79 wake turns:

| device | median | p90 | max | RSSI |
|---|---|---|---|---|
| Office | 264ms | 272ms | 278ms | −25 |
| Lounge | 260ms | 294ms | 952ms | −52 |
| Retreat | 258ms | 1046ms | 1225ms | −66 |

The tail tracks RSSI, so any deadline must clear Retreat's worst case, not
Office's typical. See §6 Q3 — because the deadline costs nothing in the happy
path, the answer is to set it generously rather than tightly, which makes RTT
instrumentation confirmatory rather than blocking.

Side benefit: an unconfirmed interaction is a far faster link-failure detector
than the 45s pong timeout. The button becomes an active probe, and local
feedback and connection-awareness reinforce each other instead of conflicting.

---

## 4. Event → outcome tables

State names used below: `IDLE`, `LISTENING`, `THINKING`, `PLAYING`, `MUTED`,
`VOL-DISPLAY` (2s arc window), `DISCONNECTED`, `PENDING`.

### 4.1 Action (dot) button — clickType 138

| # | State | Link | Device action | Ring outcome | Controller | Status |
|---|---|---|---|---|---|---|
| A1 | IDLE | LINKED | Send button event | *Nothing until controller replies* — ring lights one RTT later | Starts turn, sends `led_anim` listening | [today] |
| A2 | IDLE | LINKED | Send button event **+ paint listening ring locally**, arm confirmation deadline | Listening ring **immediately** | Confirms with `led_anim` listening, replacing provisional paint | [proposed] |
| A3 | IDLE | LINKED, no confirmation before deadline | Withdraw provisional paint, mark link DOWN | **Hard cut** to orange pulse — no fade (§6 Q4) | — | [proposed] |
| A4 | IDLE | DOWN / SUSPECT | Do not send, do not paint a turn state | Orange pulse continues (unchanged) | — | [proposed] |
| A5 | LISTENING / THINKING / PLAYING | LINKED | Send button event | Ring clears when controller's cleanup arrives | Cancels turn (`cancel_event` + `speaker_flush`); **local only — HA's pipeline runs to completion, result discarded** (`em_esphome.py:1158`) | [today] |
| A6 | LISTENING / THINKING / PLAYING | LINKED | Send button event **+ clear ring locally** (press during a turn state is unambiguously *cancel*) | Ring clears **immediately** | Cancels as above | [proposed] |
| A7 | **MUTED** | tap | Sent with `muted: true`; **controller refuses the turn** (`em_button.decide` → `BLOCKED`) | Red ring unchanged — **silent by design** (see §6 Q1) | Nothing. Mic never opens: `mic_start` is rejected device-side while muted | [today] |
| A8 | **MUTED** | hold | Sent with `muted: true` | Red ring unchanged | **Fires `long` to HA.** A hold is not speech, so the mute has no opinion about it | [today] |
| A9 | VOL-DISPLAY | LINKED | Send button event **and cancel the arc's hold** (`CancelVolumeDisplay`) | Arc stops being sovereign; the turn's listening frame paints as soon as it arrives | Starts turn normally | [today] |

### 4.2 Mute button — clickType 113 (device-local, never leaves the device)

| # | State | Device action | Ring outcome | Controller | Status |
|---|---|---|---|---|---|
| M1 | IDLE | ADC mute all 4 codec pairs, persist to state.json, button LED on | Solid red; **suppresses all controller paints** | Notified via `mute_state` | [today] |
| M2 | LISTENING / THINKING / PLAYING | As M1 | Solid red immediately; the cancelled turn's LED cleanup arrives *after* and is correctly ignored | Cancels turn + `speaker_flush` | [today] |
| M3 | MUTED (unmute) | ADC unmute, clear ring, button LED off, persist | Ring black; next controller frame repaints | Notified via `mute_state` | [today] |
| M4 | VOL-DISPLAY | As M1 — red painted directly | Red wins (painted last); arc expiry is mute-aware and restores red | Notified | [today] |
| M5 | DISCONNECTED | As M1 — **works with no controller at all** | Solid red replaces orange pulse | None | [today] |
| M6 | Boot after reboot/OTA while muted | `RestoreMuted()` — ADC + flag pre-connect; ring + button LED once LED init completes | Red, before any controller contact | None | [today] |

Mute is the reference implementation of principle 5, and its behaviour is
**not changing**.

### 4.3 Volume buttons — clickType 115 / 114 (device-local)

| # | State | Device action | Ring outcome | Status |
|---|---|---|---|---|
| V1 | IDLE | `Set(level, showRing=true)` | Cyan arc 2s → black | [today] |
| V2 | LISTENING / THINKING / PLAYING | As V1 | Cyan arc 2s → hands back to the live animation mid-frame | [today] |
| V3 | MUTED | As V1 | Cyan arc 2s → **red ring restored** (expiry is mute-aware) | [today] |
| V4 | DISCONNECTED | As V1 | Cyan arc 2s → orange pulse resumes | [today] |
| V5 | Remote set (controller / HA) or boot-time `SeedVolume` | `Set(level, showRing=false)` | **No arc** — nobody is at the device | [today] |

### 4.4 Controller-originated ring messages

| # | Message | State | Ring outcome | Status |
|---|---|---|---|---|
| C1 | `led_anim` listening (`solid`, `listening:true`) | any unsuppressed | Scene listening colour; enables direction overlay | [today] |
| C2 | `led_anim` `spin`/`rotate` (thinking) | any unsuppressed | Spinner on device ticker, 80ms | [today] |
| C3 | `led_anim` `meter` (playing) | any unsuppressed | Throbs with live speaker RMS at the ALSA write — the **voice plane only**, measured before the music mix (v2.10.0). The AEC far-end tap deliberately sees the mixed output, since that is what must be cancelled from the mic; the meter must not, or it throbs to a song nobody asked it to visualise | [today] |
| C4 | `led_anim` `off` | any unsuppressed | Ring black | [today] |
| C5 | Any `led_anim` / `leds` | MUTED or VOL-DISPLAY | **Recorded into `baseLEDs`, not painted** | [today] |
| C6 | Legacy `leds` frame | any unsuppressed | Atomically replaces any running animation (generation counter) | [today] |
| C7 | No replacement within `ttlSec` | animation running | Dead-man clears the ring — protects against a controller that died mid-turn | [today] |

### 4.5 Audio / link lifecycle

| # | Event | State | Ring outcome | Status |
|---|---|---|---|---|
| L1 | Control WS registered | PENDING / DISCONNECTED | Pulse stops; mute ring restored if muted, else hand back | [today] |
| L2 | Control WS closed (read-loop error) | any | `StopAnim()` then orange pulse | [today] |
| L3 | Awaiting admin approval | boot | White slow pulse | [today] |
| L4 | Link silently dead | any | **Nothing for up to 45s** — ring keeps showing the last state | [today] |
| L5 | Link silently dead | any | Detected on the next interaction (§3) or by inbound-freshness timeout | [proposed] |
| L6 | **Speaker stream ends** (EOS received *and* audio channel empty) | PLAYING | **Controller estimates this from wall-clock and clears the ring early on slow links** — measured up to 6.1s premature | [today] |
| L7 | Speaker stream ends | PLAYING | Device clears / hands back the ring itself, from the signal it already logs (`pcm_speaker.go:309`) | [proposed] |

---

## 5. Why L6 is wrong today

`_run_post_turn_playback` never learns when playback actually ended
(`em_controller.py:746`):

```python
audio_duration = len(speaker_pcm) / (SPEAKER_RATE * 2) + SPEAKER_PRIME_SECONDS
remaining      = max(0.0, audio_duration - elapsed)
```

`elapsed` is *socket-write* time and is **subtracted**, so the estimate shrinks
on precisely the streams that need it longest. Measured against `delivery_ms`
(the device's `playback_stats` arrival — a true end-of-audio signal) across the
last 40 instrumented turns, 4 cleared the ring more than 0.5s early:

| turn | tts | send_ms | delivery_ms | recv_span | ring cleared |
|---|---|---|---|---|---|
| 07-24 23:53 Retreat | 2.6s | 1154 | 9764 | 8193 | **6.1s early** |
| 07-24 22:31 Lounge | 5.0s | 4296 | 9353 | 2039 | **3.2s early** |
| 07-24 05:26 Lounge | 9.6s | 5364 | 13833 | 8275 | **3.1s early** |
| 07-22 22:14 Lounge | 4.6s | 1 | 7417 | 4112 | **1.7s early** |

Every one correlates with inflated `recv_span_ms` / `max_gap_ms`. On healthy
turns the estimate is ~1s *conservative*, which is why this only surfaces
occasionally.

The device is the only party that knows when its own buffer empties. It already
detects and logs exactly that. Under principle 2 this belongs to the device,
and needs **no confirmation** — nothing is being predicted.

---

## 5b. Termination at the deadline [proposed]

When the confirmation deadline expires, the interaction is over. Four things
must happen, and the third is the one with teeth.

**1. Ring — hard cut to orange** (§6 Q4). The device is now in link state DOWN.

**2. Revert the mic locally.** By deadline time the device may already have
received `mic_start_turn` (it precedes `led_anim` in the controller's send
order), so a bounded turn stream may be running. The device must stop it and
return to the continuous wake stream — `StopMic()` then `StartMic(false)`.

**3. Tell the controller to abandon the turn.** Best-effort `turn_abort` control
message. **This is mandatory, not optional, and the reason is privacy rather
than tidiness:** without it the controller runs a full voice turn — streaming
mic audio to HA — while the device shows orange. A device that is listening with
no ring is exactly the state that must never exist. The controller releases
`voice_lock`, cancels the turn, and stops the mic stream.

The abort reaches the controller in the common case, because the common cause of
a late reply is a controller that is *slow but alive*. If the link genuinely
dropped, reconnection already resets the ring (`leds_off` on config push,
`em_controller.py:1638`) and the controller's own turn fails with the socket.

**4. Refuse late arrivals for the abandoned interaction.** Belt-and-braces for
the race where the abort and a late `led_anim` cross in flight. The device
stamps each local interaction with a monotonically increasing **interaction
epoch**, sends it with the button event, and the controller echoes it on the
turn's ring messages. An `led_anim` carrying an epoch older than the device's
current one is discarded.

This is the same generation-counter pattern the animator already uses
(`animator.go:45`), extended across the wire — a stale response can never paint
over its successor. Blanket-ignoring `led_anim` for a period would be wrong: it
would also suppress a legitimate *new* turn from a second press or a wake word.

### Interaction with the pre-existing button race

There is a latent race in `handle_button_event` that termination semantics would
turn from harmless into user-visible. The start/cancel decision reads
`device.voice_lock.locked()`, but the spawned task does not acquire the lock
until several awaits later:

```python
if device.voice_lock.locked():   # em_controller.py:1433
    ...cancel...
else:
    _btn_task = asyncio.create_task(_button_voice_turn())   # acquires the lock later
```

Two presses inside that window both see the lock free and both queue a turn. The
second turn's `led_anim` is then delayed by however long the first turn takes —
easily beyond any sane deadline. Under termination semantics the device would go
orange, abort, and the controller would still run the queued turn.

**So this fix belongs in the same change:** make the start path claim the turn
synchronously in the handler (a `turn_pending` flag set before spawning, or
acquire the lock in the handler), so the cancel/start decision is atomic. It was
not observable in the 2026-07-25 field test — presses ~700ms apart never
collided — but a fast double-tap would.

---

## 6. Decisions

### Q1 — refused press while muted: **stay silent** (decided)

The red ring plus the red mute-button LED are sufficient signal that the device
is deaf. This is how Alexa behaves, and there is no reason to reinvent
behaviour that extensive focus-group work has already settled. No re-assert
flash, no refusal signal. Row A7 stands as-is.

**Amended 2026-08-08.** The *silence* decision above is unchanged, but what
gets refused narrowed. The device used to drop every dot press while muted,
which was right while the button meant only "start a voice turn" and became
wrong once a hold also fired an HA event: a hold bound to something unrelated
to speech stopped working whenever the mic was off, with nothing on the ring
to connect the two. Presses now carry the mute state and the controller
refuses only the **turn** (row A7); a hold forwards (row A8). Mute is still
sovereign on the device, because sovereignty was never the button filter —
it is the ADC mute plus the device rejecting every `mic_start` while muted.

### Q2 — how the device knows it is mid-turn: **semantic field on `led_anim`** (recommended)

This matters only for row A6 (clearing the ring immediately on a cancel press).
Without it, the device would have to paint listening on *every* dot press,
which flashes the wrong state for one RTT whenever the press was a cancel.

**Option A — infer from the current animation.** The animator stores only
`{mu, gen}` (`animator.go:45`) — no pattern — so this needs the current pattern
recorded alongside the generation. Roughly three lines.

*Correctness today is exact:* neither `em_player` nor the announcement path
paints the ring (announcements call `_run_post_turn_playback` directly, which
has no LED calls), so "ring shows a turn state" ⟺ "a voice turn is active".

*The risk is a future foot-gun, not a present bug.* The equivalence is an
implicit contract. The day music playback gains a meter ring — a very natural
feature request — the dot button silently changes meaning during music, and
nothing fails loudly. The ring would simply start clearing on presses that
actually start turns.

**Option B — derive from device audio/mic state.** Not viable. Wake-triggered
turns deliberately keep the continuous ungated stream and never send
`mic_start_turn` (P0-1), so `micActive && lockMic` is **false during the
listening phase of the most common turn type**. And `streamActive` is also true
for music and announcements — the inverse of A's problem. Neither signal, alone
or combined, covers the state space.

**Option C — explicit semantic from the controller.** The authority for "will
my press be read as cancel?" is literally `device.voice_lock.locked()`; any
device-side inference is a replica. The controller already sends an `led_anim`
on **every** turn-state change, so adding a semantic field to that message
(`state: listening | thinking | playing | idle`) costs **zero extra traffic**
and two transitions per turn.

**Recommendation: Option C.** It keeps A's zero-cost property while removing
A's implicit contract — the device reads a declared state instead of inferring
intent from pixel colour, and a future music-meter feature cannot silently
change button semantics. It also makes the provisional paint self-describing:
the device sets its own semantic state when it paints optimistically, and the
controller's next `led_anim` either confirms or overrides it.

Staleness (one RTT) is identical for A and C and is irreducible, so it does not
differentiate them. Both drift directions self-correct within one RTT.

### Q3 — confirmation deadline: **fixed, 3–3.5s** (recommended)

Because the deadline **terminates the turn** (Q4), a false withdrawal now costs
the user a real turn, not just a flicker. The deadline is therefore *not* free,
and its floor is the worst legitimate time-to-first-inbound-message.

That worst case is one full round trip: the controller emits its first response
sub-millisecond after the button event arrives (measured — "Dot button → voice
turn" and "Voice turn starting" land in the same millisecond), so the only
variable is the wire. Retreat's uplink leg measures 1046ms p90 / 1225ms max, so
a round trip worst case is ~2.5s.

**3–3.5s** clears that with margin while remaining ~13× better than today's 45s
blindness. Erring long is still correct: over-waiting costs a slower error
indication, under-waiting kills turns the user wanted.

**Fixed, not adaptive.** Per-device adaptive deadlines add state and a tuning
surface to guard against a rare failure. Revisit only if field data shows
Retreat-class devices false-aborting.

**Confirmation is *any* inbound control message, not specifically `led_anim`.**
This matters and it is not obvious: for a button turn the controller sends
`mic_stop` → `mic_start_turn` → `led_anim`, so `mic_start_turn` is an *earlier*
proof of life than the ring message. Arming the deadline against any inbound
traffic is a strictly weaker condition, satisfied sooner, and therefore
false-aborts less. `led_anim` remains the sole authority for ring *state*;
liveness and ring-state authority are separate concerns.

*RTT instrumentation* stays confirmatory rather than blocking — validate in the
field that no legitimate first response exceeds the deadline.

### Q4 — provisional-paint withdrawal: **hard cut to orange, and terminate the turn** (decided)

A hard cut draws attention to an error state; a fade softens something that
should be noticed.

**The deadline terminates the interaction — it is not merely a repaint.** Once
the device has gone orange and told the user "the controller is not answering",
a late arrival that resurrects the turn only confuses them. The turn ends at the
deadline, and anything that arrives afterwards for that interaction is refused.

See §5b for what termination requires. Note the knock-on to Q3: because the
deadline now ends a real turn rather than just changing a colour, it is no
longer free, and its floor is set by the worst legitimate round trip.

---

## 7. Invariants — do not break

- **Mute is device-sovereign.** Correct with no controller, wedged controller,
  or lying controller. Persisted locally; survives OTA slot flips.
- **The volume arc owns the ring against *animations* for its 2s window.**
  Turn animations repaint every ~80ms and would otherwise stomp it within one
  frame. It does **not** outrank a deliberate action-button press, which
  cancels the hold — the arc is protection from repaint churn, not from the
  user (2026-07-25: pressing the button after a volume change gave no sign
  the device was listening until the window expired).
- **Arc expiry is mute-aware.** It must restore red when muted, never hand back
  to controller state.
- **`ttlSec` dead-man stays.** It is the only protection against a controller
  that dies mid-animation.
- **Animation replacement stays atomic** (generation counter). A stale
  goroutine must never paint over its successor.
- **`baseLEDs` keeps recording while suppressed.** This is what makes hand-back
  seamless.
- **The direction overlay requires `listeningLEDs`** and brightens the base
  colour — it must never paint a hardcoded green, which reads as a glitch on
  any non-standard scene.

---

## 8. Risk note

The LED priority system is the most-repaired subsystem in this codebase — the
two paint suppressions in §2 were each won the hard way, and their comments
record why. Adding a provisional-paint layer means adding a **fourth**
arbitration rule. The proposals here are deliberately staged so the
lowest-risk, fully-evidenced change (L7) can ship independently of the
provisional-paint layer (A2/A3/A6), which carries the new arbitration rule and
the `led_anim` semantic field.
