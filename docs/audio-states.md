# Audio State Model

Who owns the speaker, what is on the wire, and what happens when two things
want it at once.

This exists for the same reason `led-ring-states.md` does. Five things can now
put audio on a device — a voice response, music, an HA announcement, (with
#167) a timer alarm, and the device's own wake cue — and each was added on its
own, correct in isolation. The
interactions between them are where the open bugs live: #261 (the duck lifting
mid-response) and #262 (music deferred until a turn ends). #243 — whether the
output chain belongs device-side — was answered on 2026-08-22 and is section 8.

Status markers match the LED doc: **[today]** is shipped behaviour verified in
code, **[proposed]** is designed or in review and not merged. **[built]** is
new here and means written, tested and merged but **not yet heard on
hardware** — a distinction worth keeping, because most of section 8 landed on
2026-08-22 and nobody has listened to any of it.

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
| 3 | Timer alarm ring | voice | `start_timer_alarm()`, bursts gated on `speaker_busy` | dismissal (button / spoken / CANCELLED) or `MAX_RING_S` = 120s | **[proposed #167]** |
| 4 | Media / music | music | `em_player.play()` | `stop()` / `pause()` / device gone |
| — | **Device cue** (wake confirmation) | *neither* | `PcmSpeaker.PlayCue()`, summed at the ALSA write | plays out, ~245ms | **[today — 3.0.0]** |

**The cue sits OUTSIDE the ladder on purpose** (#120). It is not a stream and
takes no ownership: no prime gate to delay it, no EOS, no flush to discard it,
no underrun accounting. It is a couple of hundred milliseconds the device
generated itself and can always deliver, summed into whatever is already
playing. Putting it through `audioStream` would mean the confirmation that the
device is listening could be held back by the prime gate or thrown away by a
barge-in flush — both wrong for a cue whose entire job is to be immediate.

It is summed **before** the output chain, so it is EQ'd and limited with
everything else and can never push the sum into clipping when it lands on top
of loud music.

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
| F5 | alarm dismissed | `speaker_flush` | otherwise the ring plays out of ~5.5s of device buffer | **[proposed #167]** |

The gate for F1/F2 is `em_player.py:481`. **A voice turn must never send
`music_flush`** — the device's own handler says so (`control.go:520`) and it is
the whole reason the second plane exists.

### 5.3 Timer alarm **[proposed — #167]**

| # | Precondition | Action | Status |
|---|---|---|---|
| T1 | HA sends `TIMER_FINISHED` | ring starts: looped bursts + amber LED pulse if `led_anim_capable` | [proposed] |
| T2 | a turn or announcement is playing | burst held off while `device.speaker_busy` is non-zero | [proposed] |
| T3 | wake word heard over the ring | alert ducked by `DUCK_DB` for `DUCK_HOLD_S` = 12s so the command reaches STT | [proposed] |
| T4 | dismissal (button, transcript, or `CANCELLED`) | ring stops, `speaker_flush` | [proposed] |
| T5 | nobody answers | stops at `MAX_RING_S` = 120s | [proposed] |

`speaker_busy` is a counter rather than a flag because an announcement can
overlap a turn's playback, and it is held in a `try/finally` because a
cancelled turn that leaked it would block every future ring for the life of the
process.

---

## 6. Sendspin **[proposed — #89]**

Synchronised multi-room playback via the Open Home Foundation's Sendspin
protocol, which Music Assistant speaks natively (WebSocket, port 8927,
`/sendspin`). Placement decided 2026-08-22: **the client runs on the device
and talks to Music Assistant directly**, not in the controller.

**This section is expected to take several passes before it settles**, and is
written to be edited rather than to look finished. What carries a date and a
name is decided; everything else is open, and a decision recorded here can be
revisited — the point of writing them down as they are made is that the next
pass starts from the current position instead of re-deriving it. Nothing here
has been built yet.

### 6.1 It is a second producer, not a third plane

This is the part to get right before any code. Sendspin carries the same
thing the music plane already carries — Music Assistant audio — by a
different route:

```
today      MA → HA → controller (em_player) → 0x04 → device music stream
sendspin   MA ─────────────────────────────────────→ device music stream
```

So **no new frame type, no new mixer input, no new row in the ownership
ladder**. The music `audioStream` gains a second thing that can fill it, and
everything worked out for the first producer — the duck, `music_flush`,
saturating mix, prime gate, underrun accounting — applies unmodified. The
work is a client and a clock, not an audio path.

The alternative (controller runs the client, re-streams over `0x04`) was
rejected: our plane carries no timestamps, so playback would land whenever
the ~5.5s device buffer happened to drain it. Sample-accurate sync is the
entire point of the protocol, and it cannot survive that hop.

### 6.2 What the device has to implement

| Piece | Requirement | Cost | Verified |
|---|---|---|---|
| Discovery | mDNS — server advertises `_sendspin-server._tcp.local`, client advertises `_sendspin._tcp.local` | we already run mDNS for `_emcontroller._tcp` | spec |
| Encryption | Spec says **mandatory** (Noise `KKpsk2`, server initiator, client responder, over plain `ws://`) — **but MA implements none of it**, see 6.5 | `flynn/noise`, pure Go | spec + MA source |
| Cipher suite | `25519_ChaChaPoly_SHA256` or `25519_AESGCM_SHA256`; servers support both, clients need one | pick ChaCha — the A53 has no AES instructions | spec |
| Codec | Servers MUST support `pcm`, `flac` and `opus`; the client advertises what it wants in `client/hello`'s `player@v1` support object | see below | spec |
| Clock | Client MUST use the time-filter algorithm (2-D Kalman) to map server timestamps onto its local clock | needs a local playback clock — we have one | spec |

**Codec: FLAC first, and Opus is not an entry cost.** The August assessment
had Opus-via-cgo (armv7a/API 22) as a blocker. It is not one — because the
server must support all three, the client may advertise FLAC only, and there
are pure-Go FLAC decoders. That is roughly half of PCM's bitrate, lossless,
and no cgo. **Settled by measurement 2026-08-22 — advertise FLAC**; 6.4 has
the numbers and the trade against PCM. Opus becomes a bandwidth optimisation
to take later, deliberately.

**The clock is the reason this is possible at all.** Measured 2026-08-10 off
`/proc/asound/card0/pcm23p/sub0/status`: `hw_ptr` advances in 112–144 frame
steps every ~2.4ms — sub-period by ~15×, and 47973 fps against 48000 nominal
(−0.056%). The driver reports a real DMA position rather than software
bookkeeping, so scheduled playback to ±0.5ms is plausible. Software
bookkeeping would have advanced in 2048-frame jumps every 42.7ms, and this
whole section would be impossible.

### 6.3 Ownership and arbitration

| # | Precondition | Action | Status |
|---|---|---|---|
| S1 | Sendspin session starts | device becomes the music plane's producer; controller told, so the `media_player` entity reports correctly | [proposed] |
| S2 | voice turn during Sendspin playback | unchanged — `duck on`, music attenuated by `duckDb`, response on `0x02` | [proposed] |
| S3 | controller sends `0x04` while a Sendspin session owns the plane | **HA wins** — device leaves the group cleanly, then plays `0x04`. Never interleaved (invariant 7) | [proposed] |
| S4 | Sendspin session ends | producer released, `music_flush` semantics unchanged | [proposed] |

S3 is the genuinely new failure mode. "Play some jazz" spoken to the device
runs through HA and arrives on `0x04`; a Sendspin group started from the MA
app arrives on the socket. Both are Music Assistant, both are legitimate, and
summing them is noise.

**Decided 2026-08-22 (Wil): HA wins — it is the direct user request.** A
device asked for music through HA leaves the Sendspin group and plays what it
was asked for.

Three things that rule does *not* mean, each of which would be a
misreading with real consequences:

- **It does not apply to voice.** A voice turn or an announcement never
  contends for the music plane at all — it ducks (V1, S2). This rule governs
  the two MUSIC producers only, and the whole point of the second plane is
  that the highest-priority audio on the device does not need to win this
  argument.
- **Leaving is not ignoring.** The device must end the Sendspin session
  properly rather than stop reading the socket: a server still streaming to a
  client that has silently stopped playing keeps filling a buffer nobody
  hears, and the group's view of the device stays wrong. Whatever the spec's
  clean-leave path is, that is the one to take.
- **It is not a priority ordering.** Sendspin does not sit at a fixed rung
  under HA — it owns the music plane whenever HA is not asking for it. Last
  direct request wins, which is the same shape as V4, where a user command
  during a turn overrides our auto-resume.

**No rejoin when the HA-routed music ends** (Wil, 2026-08-22). A silent
rejoin puts audio in the room nobody asked for at that moment, and the person
who started the group can start it again. Revisit if it annoys in practice —
the cost of being wrong here is an extra tap, which is the cheap direction to
be wrong in.

### 6.4 What is not yet known

- ~~**CPU.**~~ **Measured 2026-08-22 on EA Test Device 01 (v2.12.0), and it
  fits.** `device/tools/sendspin_bench` against 30s of pink noise at 607 kbps
  (40% of PCM), wall time with `GOMAXPROCS=1` on a live device, so scheduler
  contention is included rather than hidden:

  | Per second of audio | % of one core |
  |---|---|
  | FLAC decode | 3.28 |
  | ChaCha20-Poly1305 @ FLAC rate | 0.40 |
  | ChaCha20-Poly1305 @ PCM rate | 0.97 |
  | **total, advertise `flac`** | **3.68** |
  | **total, advertise `pcm`** | **0.97** (+929 kbps on the wire) |

  X25519 is 7.74ms, so the KKpsk2 handshake is ~23ms once per connection —
  irrelevant. **Advertise FLAC**: it costs 2.7 points of CPU over PCM and
  saves 929 kbps on links this fleet is already known to stutter on (4.6–7.1%
  loss measured, #139/#140). Trading a link we know is marginal against a core
  we have 3.68% of room on is not a close call.

  What this does **not** cover: WebSocket framing, the buffer scheduler, and
  the time filter itself. All small, none zero — treat 3.68% as a floor, not
  a budget.
- **32-bit.** Every tested Sendspin platform is 64-bit (Pi 3/4/5, Zero 2 W).
  We are 32-bit ARM. Nothing in the spec depends on word size, but nobody has
  run it there.
- **`sendspin-go` is a reference, not a dependency.** v1.2.0 dropped the oto
  backend to unify on malgo/miniaudio, so its output path is hardcoded to a
  cgo audio library we have no use for — we own tinyalsa and the mixer.

### 6.5 Read off Music Assistant's actual implementation

Settled 2026-08-22 by reading `aiosendspin` **6.0.5** as deployed in the live
add-on, rather than the published spec or GitHub's main — that is the code our
devices would actually talk to. Three questions closed and one constraint
found.

**MA's server implements NO encryption.** There is no Noise anywhere in
`aiosendspin` (every "noise" match is Kalman process noise in
`client/time_sync.py`), and its dependencies are `aiohttp`, `mashumaro`,
`orjson`, `zeroconf` plus `av`/`numpy`/`pillow` for the server extra — no
crypto library at all. So a plaintext `ws://` client works against MA today,
and the spec's "mandatory" is aspirational as far as this server is concerned.

**Do not architect Noise out on the strength of that.** The spec does require
it, MA may implement it later, and at 0.40% of a core it is cheap to carry.
Build the client so the handshake is a layer that can be switched on, not an
assumption baked through the transport.

**Mid-stream format renegotiation IS implemented** (`player/v1.py:696`,
`on_stream_request_format`). An active stream takes an explicit branch that
rebuilds the audio requirements and defers `stream/start` until the next audio
chunk, so the codec header rides with it. So the jack-insert plan in 6.2 —
`channels: 1` normally, `channels: 2` when a plug appears — is supported by
the server rather than merely permitted by the spec.

**⚠ The constraint that will bite: a request must EXACTLY match a format the
client advertised, and the failure is silent.** The server filters the
client's `supported_formats` from `client/hello` through
`filter_encodable_formats`, then requires the full
`(codec, sample_rate, bit_depth, channels)` tuple to be present in that list.
If it is not, it logs a warning **on the server** and silently falls back to
the base format. The client is told nothing, and simply keeps receiving what
it had.

So the device must enumerate **every** combination it might later ask for —
mono *and* stereo, and any PCM fallback — in its first `client/hello`.
Advertising only mono and later requesting stereo on jack insert would appear
to work, change nothing, and leave no trace on the device.

**`client/goodbye` carries a `reason` and is the clean-leave path** that S3
requires (`models/core.py:295`, handled at `server/connection.py:737`). The
S3 rule — HA wins, the device leaves the group cleanly — has a message to send
rather than a gap.

**One parameter to derive, not guess:** `client/hello` carries
`buffer_capacity`, "max size in bytes of compressed audio messages in the
buffer that are yet to be played". That is a statement about *our* buffer
(`audioChanDepth` ≈ 5.46s), expressed in compressed bytes, and the server's
`BufferTracker` paces against it. Getting it wrong means the server either
starves us or overruns us.

---

## 7. Open questions

- **Q1 — should music be allowed to start during a turn, on its own plane?**
  Today `play/resume/pause/stop` record intent and do not touch the wire while
  a turn owns the speaker, so a stream started from a phone sits silent until
  the answer finishes (#262). Now that music has its own plane, the reason for
  the blanket rule is weaker than when it was written. Undecided.
- ~~**Q2 — where does the output chain belong?**~~ **Answered 2026-08-22: on
  the device.** See section 8.
- **Q3 — what owns the speaker when the jack is occupied?** A plug in the jack
  degrades the whole audio subsystem (#117/#141) and, with a music session
  live, can silence everything including voice. That is a hardware/HAL fault
  rather than an ownership one, but it presents as an ownership bug and should
  be named here so it is not re-diagnosed as one.

---

## 8. The output chain moves to the device **[built — 3.0.0]**

Decided AND BUILT 2026-08-22 (Wil): **the whole output path runs on the
device, and the controller provides the config knobs to drive it.** Ported in
`device/internal/outchain`, wired in at `pcm_speaker.silenceLoop`, gated on the
`output_chain` capability. **Not yet heard on hardware** — every claim below is
verified against the reference implementation or by test, not by ear. Scope is the OUTPUT path
only — EQ, limiter, bass guard and the mix. Input-side processing
(`em_ns.py`/DTLN on the ASR-bound mic stream) stays controller-side and is a
separate workstream: it is a neural net on the path to speech recognition
rather than to the speaker, and nothing here needs it moved.

### 8.1 Why — three reasons, in the order they carry weight

- **Tuning latency collapses, from seconds to one period.** Controller-side,
  a parameter change reaches only samples not yet sent, and the music feed
  runs `LEAD_S` = 4.0s ahead into a buffer up to 5.46s deep — so a change is
  heard **at least ~4s later**, and a voice response never hears it at all,
  since the chain is built per stream. Device-side the chain sits at the ALSA
  write, so a config push lands within RTT + one period (~43ms). These are
  taste parameters tuned by ear, and four seconds of the old setting is long
  enough to blur the comparison being made.

  This is the SAME argument that forced ducking device-side (principle 3:
  audio that has left the controller cannot be un-sent). EQ tuning has the
  identical shape.
- **One chain post-mix is the correct topology, and today's cannot be
  reached.** The controller runs **two independent** chains —
  `em_controller.py:1402` for the response, `em_player.py:544` for the media
  feed — so **neither limiter ever sees voice and music summed**. Only the
  mixer's saturation stands behind that today.
- **Sendspin makes it forced rather than preferred.** A Sendspin session goes
  MA → device and never crosses the controller, so a controller-side chain
  would shape voice and HA-routed music while silently not shaping
  synchronised music — the same speaker sounding different depending on which
  app started the track.

No protocol or GUI work is implied: all seven keys (`eqBands`, `eqLoudness`,
`limiter*`, `bassGuard*`) already ride the config push and are currently
ignored by the device.

### 8.2 Requirements — all met, none heard

R1–R7 below were the acceptance criteria. What they cost, recorded because the
next port will want to know:

- **R5 (fixture agreement) came out EXACT** — all fifteen cases, error −Inf dB,
  peak difference 0 LSB, including every parameter transition and the flush
  tail. Achievable rather than lucky: keep float64, keep scipy's transposed
  direct form II, keep each expression in the reference's algebraic *shape*,
  and write the shared gain law in its arithmetic ORDER.
- **R7 (fixed point vs float) is answered: float64.** Nine biquads plus a
  crossover at 48kHz is a few Mflop/s. There was never a reason to reach for
  something narrower, and float64 is what makes the agreement provable.
- **R4 (no click) needed a measurement before a mechanism** — see 8.3.

#### The criteria

| # | Requirement | Why |
|---|---|---|
| R1 | Gate on an **`output_chain` capability**, independent of any Sendspin capability | A device could speak Sendspin without a local chain; standing the controller down for it ships unshaped audio. Same split as `oww_shadow` vs `oww_trigger` |
| R2 | Controller stands down **only** on announce; otherwise it shapes as today | Degrade to old behaviour, never to a wrong answer. Two chains in series is two limiters in series, which is audibly wrong |
| R3 | Chain sits **after the mix**, once | 8.1 |
| R4 | **No click on any parameter change** | Deal-breaker (Wil, 2026-08-22). See 8.3 |
| R5 | Port validated against **golden fixtures** generated from the Python chain | Precedent: `internal/wakeword/fixture` — the reason on-device wake word was trustworthy on arrival |
| R6 | Python chain **kept**, as reference implementation and old-firmware fallback | Not scaffolding to delete |
| R7 | Fixed point vs float **measured, not assumed** | The mixer's Q15 precedent covers a gain multiply; a limiter and a multiband guard are more precision-sensitive. The A53 has VFP, so float32 is on the table |

### 8.3 R4 — how "no click" is actually achieved

**Updating in place is necessary and NOT sufficient.** Preserving filter state
avoids the rebuild transient (the bug `em_player`'s comment describes), but it
does not avoid the transient from coefficients changing under a running
filter. And the obvious fix is a trap: **interpolating raw biquad coefficients
between two stable filters can pass through unstable intermediate states**, so
naive smoothing blows up rather than clicking.

- **Constant-slew parameter ramping**, exactly as `duckRampPeriods` already
  does for the duck — deliberately a constant slew and not a proportional one,
  so it is a duration rather than a time constant that crawls the last few
  percent.
- **Dual-instance crossfade for the biquads**: run old and new in parallel,
  equal-power crossfade over ~50–100ms, drop the old. Unconditionally stable
  because neither instance is ever interpolated. Costs one extra chain's CPU
  during the crossfade window only.
- **Limiter and bass guard are easier** — threshold and release changes are
  gain-domain and smooth naturally provided detector state carries over.

**Pinned by test, not by ear:** a step change in any parameter must produce no
sample-to-sample discontinuity above a threshold. Host-testable in Go, no
hardware required.

**MEASURED 2026-08-22, and the obvious probe detects nothing.** A parameter
change is only audible as a discontinuity when the FILTER STATE holds real
energy — a low frequency at level. On a 1kHz tone, none of these changes
produce a step above the signal's own slope:

| signal | change | raw step | crossfaded | peak |
|---|---|---|---|---|
| 60Hz @20000 | loudness on (state reset) | 53351 | 6144 | 72863 |
| 60Hz @20000 | all bands 0 → +12 | 61469 | 7759 | 113938 |
| 60Hz @20000 | low shelf +12 → −12 | 1065 | 578 | 80607 |
| 1kHz @8000 | any of the above | at or below the signal's own slope | | |

The top row is a step of **73% of the signal**, and it is the state-reset case
— so the quirk the port deliberately reproduces from `em_eq` is the single
worst offender, and the crossfade is what makes reproducing it safe rather than
merely faithful. Measured reduction: **8.7×**.

**Linear crossfade, not equal-power.** Equal-power is the reflexive choice and
is wrong here: it holds level for UNCORRELATED sources, whose powers add. These
two are the same signal through similar filters, so their amplitudes add and a
cos/sin fade would bulge by up to 3dB mid-transition — an audible swell on
every parameter change.

### 8.4 Known risk

The fixed-point (or float) port sounding **audibly different** from the Python
chain is the one item here without a known method — everything else is
engineering. It is also the failure mode this project has previous on: the DAC
clipping above unity gain read for weeks as "Piper sounds worse than stock".
Measure it early rather than last, and R5 is what makes "different" detectable
before it is a listening test.

---

## 9. Invariants — do not break

1. **Voice is never attenuated by the duck.** Only the music plane carries
   `duckTarget`.
2. **A voice turn never flushes the music plane** (F4 above).
3. **The mixer saturates, never wraps** (`mix_test.go:149` pins this).
4. **Playback completion comes from the device**, not from a duration estimate.
5. **`speaker_busy` is released in a `finally`.** [proposed #167]
6. **Frame types are direction-scoped.** `0x04`/`0x05` are not free to reuse.
7. **The music plane has exactly one producer at a time, and HA wins.**
   [proposed #89] Interleaving controller `0x04` with a Sendspin session sums
   two unrelated streams; a direct request through HA ends the Sendspin
   session rather than mixing with it, and the device leaves the group
   **cleanly** rather than going quiet on it (S3). Invariant 2 gains a second
   reason here: flushing music for a voice turn would drop audio a
   synchronised group is counting on and force a resync.
