# Audio State Model

Who owns the speaker, what is on the wire, and what happens when two things
want it at once.

This exists for the same reason `led-ring-states.md` does. Four things can now
put audio on a device — a voice response, music, an HA announcement, and (since
#167) a timer alarm — and each was added on its own, correct in isolation. The
interactions between them are where the bugs live.

Two of those are now fixed, and the fix is the same shape both times: **owners
are COUNTED, not flagged.** #261 lifted the duck mid-response because `ducked`
was one boolean and the first of two overlapping owners to finish popped it;
#314 released the turn's speaker ownership for the same reason. `duck_depth`
and `owner_depth` are separate counters on purpose — the duck only increments
on the mixing path, so a device that pauses instead of ducking has overlapping
owners with no duck depth, and collapsing them would be correct everywhere
except on exactly those devices.

Still open: #262 (music deferred until a turn ends) and the state map the
alarm is owed as a fourth owner. Sendspin (section 6) adds a second producer
for the music plane without adding an owner.

Status markers match the LED doc: **[today]** is shipped behaviour verified in
code, **[proposed]** is designed or in review and not merged, **[built]** is
written, tested and merged but not yet heard on hardware.

---

## 1. Design principles

These are settled and the rest follows from them.

- **The device mixes; the controller decides.** Two independent PCM streams
  reach the device and are summed at the ALSA write
  (`speaker/pcm_speaker.go:277`). The controller never mixes.
- **Voice is never attenuated.** Ducking lowers the bed under it, never the
  response itself.
- **Audio that has left the controller cannot be un-sent.** `LEAD_S` = 4.0s of
  music is already in the device's buffer when a wake word fires, against a
  device depth of `audioChanDepth` = 128 × 42.7ms ≈ **5.46s**
  (`pcm_speaker.go:37`). Anything the controller wants to change about audio
  already in flight needs a control message, not a change of what it sends.
- **A voice turn ducks music; it does not pause it.** Pausing needs a seek to
  resume and a Music Assistant flow stream cannot seek, so a 28s turn cost 28s
  of the song.
- **The end of audio is what the device says it is.** Playback completion waits
  for the device's `playback_stats`, never an estimate from the socket write —
  which completes near-instantly however slow the link is (measured 2026-07-24:
  the ring cleared 6.1s early on one device, 3.2s on another).

---

## 2. Speaker owners — priority ladder

Highest first. Only one owner drives the **voice plane** at a time; music runs
underneath on its own plane and is ducked rather than displaced.

| # | Owner | Plane | Takes ownership by | Releases on |
|---|---|---|---|---|
| 1 | Voice turn (wake word or button) | voice | `em_player.interrupt()` — **unconditional**, even with nothing playing | `resume_interrupted()` at turn end |
| 2 | HA announcement | voice | same `interrupt()` path | announcement playback completes |
| 3 | Timer alarm ring | voice | `start_timer_alarm()`, bursts gated on `speaker_busy` | dismissal (button / spoken / CANCELLED) or `MAX_RING_S` = 15 min (Voice PE's) | **[today]** |
| 4 | Media / music | music | `em_player.play()` | `stop()` / `pause()` / device gone |

**Ownership is taken unconditionally, and that is deliberate** — not an
optimisation to remove. "Play some jazz" runs the intent *before* HA generates
the spoken reply, so `play_media` can arrive while the TTS is still coming. If
ownership were conditional on something already playing, the music would land
on the same plane as the response and talk over it. This is also the direct
cause of #262, and the fix there is to let music start on its own plane rather
than to weaken this rule.

---

## 3. The two planes

| Byte | Direction | Meaning | Constant |
|---|---|---|---|
| `0x01` | device → controller | Mic PCM | — |
| `0x02` | controller → device | Voice PCM (48kHz mono S16_LE) | `frameTypeSpeaker` |
| `0x03` | controller → device | Voice end-of-stream | `frameTypeEOS` |
| `0x04` | controller → device | **Music PCM** | `frameTypeMusic` |
| `0x05` | controller → device | **Music end-of-stream** | `frameTypeMusicEOS` |
| `0x04` | device → controller | **VAD end-of-speech** | `frameTypeVADEnd` |
| `0x05` | device → controller | **No-speech timeout** | `frameTypeNoSpeechTimeout` |

**`0x04` and `0x05` mean different things in each direction** and are
disambiguated only by which way the frame is travelling
(`device/internal/client/data.go:27-45`). Nothing enforces that beyond the
reader being on one end of the socket. Worth knowing before adding a frame
type.

The device holds one `audioStream` per plane, each `audioChanDepth` deep, and
mixes them at the write with the current duck gain applied to music only
(`pcm_speaker.go:123-125`, `:277`). The sum **saturates rather than wraps** — a
wrap turns a loud peak into a full-scale opposite-polarity one, far worse than
clipping.

---

## 4. Capability degradation — `audio_mix`

`em_player._frame_types()` picks the plane from the device's announced
capability (`em_player.py:70`):

| Firmware | Music plane | Voice turn does | Consequence |
|---|---|---|---|
| announces `audio_mix` | `0x04`/`0x05` | **ducks** (`duck on`) | music continues quietly under the response | **[today]** |
| does not | `0x02`/`0x03` | **pauses**, `resume_after` set | old behaviour, seek needed to resume | **[today]** |

Degrading to the old path rather than to a wrong answer is the rule from
`CLAUDE.md`: a device that cannot mix would never play `0x04` at all, which is
silence, not degraded behaviour.

---

## 5. Transitions

### 5.1 Voice turn over music

| # | Precondition | Action | Music | Voice | Status |
|---|---|---|---|---|---|
| V1 | music playing, `audio_mix` | `interrupt()` sets `ducked`, sends `duck on`, feed lead drops `LEAD_S` 4.0s → `TURN_LEAD_S` 1.0s to yield the shared data plane | continues, attenuated by `duckDb` (default −18dB) | response on `0x02` | [today] |
| V2 | music playing, no `audio_mix` | `interrupt()` → `pause()`, `resume_after = True` | stops, bookmarked | response on `0x02` | [today] |
| V3 | turn ends | `resume_interrupted()` releases ownership, `duck off` | back to unity | — | [today] |
| V4 | user command during the turn | recorded as `pending`; **overrides** our auto-resume | last write wins | — | [today] |
| V5 | duck released before the response finishes | — | **lifts early, competes with the tail** | — | **bug, #261** |

**V5 is #261 and is unexplained.** `em_player` logs only the failure paths
(`duck failed` / `unduck failed`), so a duck that is sent, applied, and then
released early is completely silent in the log. Add the log line before
theorising: "the duck never went out" and "the duck went out and something
released it" want opposite investigations.

### 5.2 Stop and flush

| # | Situation | Message | Why | Status |
|---|---|---|---|---|
| F1 | user stops/pauses music, `audio_mix` | `music_flush` | discards the buffered *music* only | [today] |
| F2 | user stops/pauses music, no `audio_mix` | `speaker_flush` | music is on the voice plane there | [today] |
| F3 | barge-in during a response | `speaker_flush` | cuts the buffered response; the rest is usually still in TCP, so the device discards until it sees the stream's `0x03` | [today] |
| F4 | voice turn starts over music | **neither** | flushing would discard the buffered audio that makes ducking instant, and on a non-seekable stream it is gone for good | [today] |
| F5 | alarm dismissed | `speaker_flush` | otherwise the ring plays out of ~5.5s of device buffer | **[today]** |

The gate for F1/F2 is `em_player.py:481`. **A voice turn must never send
`music_flush`** — the device's own handler says so (`control.go:520`) and it is
the whole reason the second plane exists.

### 5.3 Timer alarm **[today — #167]**

| # | Precondition | Action | Status |
|---|---|---|---|
| T1 | HA sends `TIMER_FINISHED` | ring starts: looped bursts + amber LED pulse if `led_anim_capable` | [today] |
| T2 | a turn or announcement is playing | burst held off while `device.speaker_busy` is non-zero | [today] |
| T3 | wake word heard while any Echo rings | every ring held silent (flush) for up to `DISMISS_LISTEN_S` + 2s while the controller listens for speech; no turn starts | [today] |
| T4 | dismissal (button, speech after the wake word, or `CANCELLED`) | ring stops, `speaker_flush`; with no speech the hold is released and the ring resumes | [today] |
| T5 | nobody answers | stops at `MAX_RING_S` = 15 min | [today] |

`speaker_busy` is a counter rather than a flag because an announcement can
overlap a turn's playback, and it is held in a `try/finally` because a
cancelled turn that leaked it would block every future ring for the life of the
process.

---

## 6. Sendspin — a second producer for the music plane **[built — EA, not yet heard on hardware]**

Synchronised multi-room playback via the Open Home Foundation's Sendspin
protocol, which Music Assistant speaks natively. Placement decided 2026-08-22
(Wil): **the player runs on the device and Music Assistant talks to it
directly**, not through the controller. Designed in PR #271, built
2026-09-30 as `device/internal/sendspin`, switched per Echo by
`sendspinEnabled`, shipped as Early Access firmware first.

### 6.1 It is a second producer, not a third plane

Sendspin carries what the music plane already carries — Music Assistant
audio — by a different route:

```
0x04       MA → HA → controller (em_player) → 0x04 → device music plane
sendspin   MA ─────────────────────────────────────→ device music plane
```

So no new frame type, no new mixer input, no new row in the ownership ladder.
The duck, the saturating mix, the output chain and software volume apply to
it unchanged, because it enters at the same `Mixer.Mix` input as `0x04`.

The one difference is timing, and it decided the shape. `0x04` is
push-and-play-in-order: the device plays each period when the one before it
is done, and nothing on the plane carries a timestamp. Sendspin is useless
without placing every sample at a moment the server chose. So the player is
a **pull source** (`PcmSpeaker.SetMusicSource`): each period, the write loop
reads the substream's ALSA `delay`, works out when the period it is building
will reach the DAC, and asks the player for the audio due then. Only that
loop knows the answer; a push into the existing buffer would lose it.

The alternative — the controller runs the client and re-streams over `0x04`
— was rejected on the same grounds: the timestamps cannot survive that hop.

### 6.2 What the device implements

| Piece | Implementation | Verified against |
|---|---|---|
| Connection | Server-initiated: the device listens on 8928 at `/sendspin` and advertises `_sendspin._tcp` (zeroconf). It never dials out, as the spec requires of an advertising client. On emOS, whose init drops inbound connections, the firmware opens 8928 in the filter before advertising and closes it when the player stops (`internal/firewall`) | aiosendspin 9.1.1 `connect_to_client`; 15LE |
| Encryption | Noise `KKpsk2`, `25519_ChaChaPoly_SHA256`, device as responder (`flynn/noise`). The PSK is chosen between the two handshake messages, after message 1 names it | 9.1.1, both first handshake and in-band re-handshake |
| Identity | X25519 keypair in `/data/local/etc/echomuse/sendspin.json`; its public key is the `client_id`. Losing the file unpairs the device from every server | spec vectors |
| Pairing | The Pairing PSK method only (the one a client must offer): the dashboard shows the `SP:0…` token, the user pastes it into Music Assistant. Unpaired access is a setting, off by default | 9.1.1 `initiate_pairing`; spec token vector |
| Clock | The spec's 2-D Kalman time filter, ported line for line | aiosendspin's own filter, to the microsecond over 300 steps |
| Codec | FLAC only, 48kHz 16-bit **mono** — one format, so the server resamples and downmixes and the device never switches output mid-stream | aiosendspin's own FLAC encoder, bit-exact |
| DAC position | `OutputClock`: an alpha-beta tracker over the per-period ALSA delay reading — least-squares at first, narrowing to fixed gains | host test, ±2ms noise |
| Correction | The spec's suggested strategy: whole-frame drops/repeats, ≤8 per 2048-frame period (0.39%, under the 0.5% cap); a one-shot resync past 5ms | host test, DAC ±600ppm |

**Interop, 2026-09-30** (`device/tools/sendspin_interop`, against the library
Music Assistant ships): pairing by token and the re-key to the long-term PSK;
worst sync error **225µs** with the DAC 560ppm off (spec floor 1ms, target
0.5ms), measured on the server's own clock rather than self-reported; volume
on the spec's curve; a seek; HA taking the plane; reconnect under the stored
PSK; unpaired access off and on.

**On hardware, 2026-09-30** (15LE, emOS 32-bit, Music Assistant 2.10.4):
paired by token; play, duck under a voice reply, seek, volume from both ends,
pause/resume, and discovery with no address entered. The first attempt found
emOS's inbound filter: the player advertised and logged "listening" while
every connection timed out, and Music Assistant never listed it.

**On hardware, 2026-10-01** (15LE emOS 32-bit and C95 emOS 64-bit, grouped in
Music Assistant, on-device wake word on both): in sync by ear with one Echo at
each ear; self-reported sync error 0.1–0.4ms, no underruns or late drops,
~9s buffered. Corrections settle at 2–7 a minute; 15LE ran ~300 a minute for
the first two minutes after a restart, then settled. Stopping the controller
stopped both players in the same second, the port closed, and both resumed
~20s after it returned, with Music Assistant restarting playback itself. The
firmware logs `[sendspin] playing:` once a minute with these counts.
A stereo pair works with the mono-only player: set one Echo to the left
channel and the other to the right in Music Assistant, and it selects each
player's channel before encoding, so each Echo receives its own side as mono
(confirmed by ear with a left/right test track).

CPU: 3.68% of one core for FLAC decode and ChaCha20-Poly1305 (PR #271's bench
on a Dot). On hardware, playing and paused measured the same within noise
(/proc ticks over 10s), so the player's cost is below what that resolves.

### 6.3 Ownership and arbitration

| # | Precondition | Action | Status |
|---|---|---|---|
| S1 | Sendspin session starts | player queues audio; the write loop pulls it while `0x04` is empty | [built] |
| S2 | voice turn during Sendspin playback | unchanged — local duck at the wake crossing, then the controller's `duck on` (sent on every turn to `audio_mix` devices, whether or not it thinks music is playing) | [built] |
| S3 | controller sends `0x04` while a Sendspin session plays | **HA wins** (Wil, 2026-08-22). The `0x04` plane is consulted first, so HA's audio takes the speaker at once; within 100ms the player reports `available: false`, clears its buffer, and the server moves it out of its group | [built] |
| S4 | Sendspin stream ends or clears | buffer cleared; `stream/clear` resyncs in place | [built] |

**No rejoin when the HA-routed music ends** (Wil, 2026-08-22), and the
protocol agrees: a server must not rejoin a client that went unavailable. The
person restarts the group. The cost of being wrong is one tap.

**One volume** (Wil, 2026-09-30). A server's volume command sets the Echo's
own volume, the one HA and the buttons move, mapped 0-100 onto 0..127 by the
same proportion as HA's percentage; every change by any route is reported
back, so Music Assistant's slider follows. The synced music itself then plays
at unity, and the level is applied once, by the speaker's software volume.
The server's mute stays a mute of synced music only: the Echo's mute button
is the microphone.

**It follows the controller link** (Wil, 2026-09-30): EchoMuse is one system,
and an Echo whose ring says it is disconnected should not be playing. The
player stops (goodbye `restart`) whenever the ring shows the link down —
disconnected, pending or refused — and starts on the next config push. The
interop test confirms Music Assistant redials by itself after that goodbye.

Not built: telling Home Assistant's media player entity that the Echo is
playing synced music (S1's "controller told"). It shows idle meanwhile.

### 6.4 Where aiosendspin 9.1.1 and the spec disagree

The device has to work with the library Music Assistant runs, and in several
places that is not what the spec (main, 2026-09-17) says. The device sends
9.1.1's form and accepts both wherever the two can be told apart:

- **Fragments**: 9.1.1 uses type bytes 2 ("more") and 3 ("end"); the spec a
  type 1 with a flags byte. Both are accepted; the device never sends one.
- **Audio chunk header**: 9 bytes in 9.1.1, 13 in the spec (adds
  `send_ahead`). Told apart by where the FLAC frame sync sits — one reason
  the device advertises FLAC only.
- **`supported_pair_methods`**: a list in 9.1.1, an object in the spec.
  9.1.1 rejects the object, so the list is sent.
- **Pairing PSK flow**: `client/pair-finalize` alone in 9.1.1; the spec puts
  `client/pair-init` first, which 9.1.1 rejects as out of sequence.
- **After a re-handshake** 9.1.1 re-sends `server/hello` and waits for a new
  `client/hello`; the spec says neither is re-sent. The device answers one if
  it comes, and says nothing else (no `client/time`) from the start of the
  exchange until the `server/activate` that ends it — a stray message there
  fails the pairing.
- **`available: false`** means "an external source has the speaker" to 9.1.1,
  which ungroups the client. The spec also says never to report `true` before
  the clock converges. Both hold by delaying the first `client/state` until it
  has (~0.4s, inside 9.1.1's 5s allowance).
- **Delay field**: `static_delay_ms` / `set_static_delay` in 9.1.1,
  `output_delay_ms` / `set_output_delay` in the spec. Both commands accepted.

When Music Assistant moves to a newer aiosendspin, re-run the interop harness
against it first: the list-shaped `supported_pair_methods` and the pairing
sequence are the two that cannot be sent both ways.

### 6.5 What is not yet known

- **The crackle was not the corrections (settled 2026-10-02, #707).** It was
  tinyalsa's `pcm_write` dropping the rest of a write a signal interrupted —
  ~15 an hour per Echo, on every playback path — fixed by blocking signals
  around the write (GoTinyAlsa fork, #711). The same dropped audio was what
  made the tracker's "early readings" and the correction bursts; with it
  fixed and the tracker gated (#710), corrections hold at 2–3 a minute and
  the worst minute overnight was 9. A correction is still a hard splice; a
  crossfade is only worth doing if one is ever heard.
- **3–10 tracker resets an hour remain**, an early jump past 20ms with every
  write whole, about 3× as often on FireOS 6's kernel. Harmless at that rate;
  unexplained.
- **Sync against another brand's player.** Every Echo shares the same fixed
  DAC latency, so Echoes agree with each other; another player needs
  `static_delay_ms` set by ear.

---

## 7. Open questions

- **Q1 — should music be allowed to start during a turn, on its own plane?**
  Today `play/resume/pause/stop` record intent and do not touch the wire while
  a turn owns the speaker, so a stream started from a phone sits silent until
  the answer finishes (#262). Now that music has its own plane, the reason for
  the blanket rule is weaker than when it was written. Undecided.
- ~~**Q2 — where does the output chain belong?**~~ **Answered: on the
  device**, at the ALSA write, whenever both halves announce `output_chain`
  (#243). It runs on the mix, so synced music passes through it too; behind a
  controller without `output_chain`, Sendspin music is not shaped at all,
  since it never crosses the controller.
- **Q4 — DECIDED 2026-09-07: the alarm never waits, the announcement does
  (#373).** Both write `0x02` today and only `_ring_timer_alarm` asks first,
  which is backwards: a timer must go off exactly when it ends, so the writer
  whose timing is the whole point is the one currently deferring. Measured
  2026-08-28: an announcement landing between chime bursts plays, one landing
  during a burst is inaudible.

  The rule is **silence the music in favour of the alarm, duck the alarm in
  favour of the response**. That resolves the three-way case — music playing,
  turn active, timer fires — without new firmware: `Mixer.Mix(voice, music,
  target)` takes exactly two inputs and attenuates only the music side, so the
  alarm rides the music plane, music is suspended while it rings, and the
  device's existing duck does the rest. Gated on `audio_mix`, since firmware
  without it never plays `0x04` and a silent timer is the worst available
  failure; those devices keep `0x02` with the alarm taking the plane.

  The announcement waits for a response to finish and queues behind other
  announcements, with a cap. Blocking it for the whole ring is still wrong —
  HA holds `_is_announcing` and 15 minutes of `MAX_RING_S` would fail every other
  announcement — but that is the UNBOUNDED wait. Waiting for the burst in
  flight is under two seconds, and reading the warning as forbidding both is
  why this sat open.

  Open: the alarm's duck depth wants its own value rather than `duckDb`, which
  was tuned for a music bed under speech; and music resumption after dismissal
  rejoins the live edge on a non-seekable stream, so a 30s alarm costs 30s of
  the track.

  **The shared `playback_done` Event is fixed** (#481): it is now a FIFO queue
  of per-playback waiters, so one device report no longer satisfies two.

- **Q3 — what owns the speaker when the jack is occupied?** A plug in the jack
  degrades the whole audio subsystem (#117/#141) and, with a music session
  live, can silence everything including voice. That is a hardware/HAL fault
  rather than an ownership one, but it presents as an ownership bug and should
  be named here so it is not re-diagnosed as one.

---

## 8. Invariants — do not break

1. **Voice is never attenuated by the duck.** Only the music plane carries
   `duckTarget`.
2. **A voice turn never flushes the music plane** (F4 above).
3. **The mixer saturates, never wraps** (`mix_test.go:149` pins this).
4. **Playback completion comes from the device**, not from a duration estimate.
5. **`speaker_busy` is released in a `finally`.** [today]
6. **Frame types are direction-scoped.** `0x04`/`0x05` are not free to reuse.
7. **The music plane has exactly one producer at a time, and HA wins.**
   The write loop pulls the Sendspin player only while `0x04` is empty, and
   the player reports itself taken while `0x04` has anything in it. Summing
   the two would be two unrelated streams at once. Invariant 2 gains a second
   reason here: flushing music for a voice turn would drop audio a
   synchronised group is counting on and force a resync.
