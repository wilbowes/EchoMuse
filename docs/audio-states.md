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

Still open: #262 (music deferred until a turn ends), #243 (whether the output
chain belongs device-side at all), and the state map the alarm is owed as a
fourth owner.

Status markers match the LED doc: **[today]** is shipped behaviour verified in
code, **[proposed]** is designed or in review and not merged.

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
| 3 | Timer alarm ring | voice | `start_timer_alarm()`, bursts gated on `speaker_busy` | dismissal (button / spoken / CANCELLED) or `MAX_RING_S` = 120s | **[today]** |
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
| T3 | wake word heard over the ring | alert ducked by `DUCK_DB` for `DUCK_HOLD_S` = 12s so the command reaches STT | [today] |
| T4 | dismissal (button, transcript, or `CANCELLED`) | ring stops, `speaker_flush` | [today] |
| T5 | nobody answers | stops at `MAX_RING_S` = 120s | [today] |

`speaker_busy` is a counter rather than a flag because an announcement can
overlap a turn's playback, and it is held in a `try/finally` because a
cancelled turn that leaked it would block every future ring for the life of the
process.

---

## 6. Open questions

- **Q1 — should music be allowed to start during a turn, on its own plane?**
  Today `play/resume/pause/stop` record intent and do not touch the wire while
  a turn owns the speaker, so a stream started from a phone sits silent until
  the answer finishes (#262). Now that music has its own plane, the reason for
  the blanket rule is weaker than when it was written. Undecided.
- **Q2 — where does the output chain belong?** EQ, limiter and bass guard all
  run controller-side today, on the voice plane, before the audio reaches the
  wire (#243). Music does not go through them at all.
- **Q4 — the alarm and an announcement both write `0x02`, and only one of
  them asks first (#373).** `_ring_timer_alarm` waits on `speaker_busy`
  before every burst, for the stated reason that two writers would interleave
  frames; `_standalone_play` performs no such check and streams into a chime
  already in flight. Measured 2026-08-28: an announcement landing between
  bursts plays, one landing during a burst is inaudible. Both paths also
  share a single `playback_done` Event, so one device report satisfies two
  waiters — observed as two `Playback complete` lines in the same
  millisecond, and an announcement whose wait ended after a chime's duration
  rather than its own.
  **The exclusion is one-directional, which is the bug**; whether the silence
  itself is EOS ordering on the device (§3, `stream_speaker`) is unconfirmed.
  Blocking announcements outright while ringing is NOT the answer — HA blocks
  on the announce call holding `_is_announcing`, so a 120s `MAX_RING_S` would
  fail every other announcement to that satellite meanwhile. The longer-term
  answer is likely the alarm moving to the music plane, where the device's
  own mixer ducks it for free; that needs `audio_mix` gating, since a device
  without it never plays `0x04` and the alarm would be silent.
- **Q3 — what owns the speaker when the jack is occupied?** A plug in the jack
  degrades the whole audio subsystem (#117/#141) and, with a music session
  live, can silence everything including voice. That is a hardware/HAL fault
  rather than an ownership one, but it presents as an ownership bug and should
  be named here so it is not re-diagnosed as one.

---

## 7. Invariants — do not break

1. **Voice is never attenuated by the duck.** Only the music plane carries
   `duckTarget`.
2. **A voice turn never flushes the music plane** (F4 above).
3. **The mixer saturates, never wraps** (`mix_test.go:149` pins this).
4. **Playback completion comes from the device**, not from a duration estimate.
5. **`speaker_busy` is released in a `finally`.** [today]
6. **Frame types are direction-scoped.** `0x04`/`0x05` are not free to reuse.
