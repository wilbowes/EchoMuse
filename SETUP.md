# EchoMuse — architecture reference

> **Looking for how to *set up* EchoMuse?** Start with the
> **[Quickstart](docs/quickstart.md)**. This page is reference material about
> how the hardware and pipeline actually work.

| If you want… | Go to |
|---|---|
| To install and use EchoMuse | **[Quickstart](docs/quickstart.md)** — the actual onboarding guide |
| To get a device rooted first | **[Rooting](docs/rooting.md)** — prerequisites, and R0rt1z2's XDA Forums thread, which is canon |
| The history: what was built, what broke, why | **[Journal](JOURNAL.md)** |
| How the mic array, audio pipeline and protocol work | This page |

This file keeps its name because things link to it from outside the
repository. Its rooting steps now live in [docs/rooting.md](docs/rooting.md)
and its journal in [JOURNAL.md](JOURNAL.md); what remains here is the
reference material on how the device actually works.

---

## Mic Array Architecture

The biscuit has a 7-microphone array captured on ALSA card 0, device 24 as 9 channels S24_3LE at 16kHz. Ch7 and Ch8 are unconnected.

```
Ch0 → MK1 → 330°  (11 o'clock)  perimeter   ← confirmed empirically 2026-05
Ch1 → MK2 →  30°  ( 1 o'clock)  perimeter
Ch2 → MK3 →  90°  ( 3 o'clock)  perimeter
Ch3 → MK4 → 150°  ( 5 o'clock)  perimeter
Ch4 → MK5 → 210°  ( 7 o'clock)  perimeter
Ch5 → MK6 → 270°  ( 9 o'clock)  perimeter
Ch6 → MK7 → centre              omnidirectional
Ch7, Ch8 → unconnected
```

**Mapping confirmed** by tone injection testing (2026-05): phone speaker pressed against each mic hole in turn, per-channel RMS measured via `analyse_capture.py`. Previous documentation had Ch0/Ch1 swapped — corrected.

**ADC architecture:** Four TLV320ADC3101 stereo ADCs (I2C bus 0, addresses 0x18–0x1b). Probe order at boot determines channel assignment: 0x18→Ch0/1, 0x19→Ch2/3, 0x1a→Ch4/5, 0x1b→Ch6/7. All chips share a TDM data bus (confirmed from PCB trace analysis — DOUT shared, not daisy-chained). Array radius: 36mm (confirmed from PCB measurement).

**Why ch6 for wake word?** The centre mic is equidistant from all directions. OWW receives consistent audio regardless of where you're standing, and ambient sounds cannot lock it to a suboptimal direction. Perimeter mics are directional by proximity — good for STT once direction is known, but wrong for always-on wake word detection.

**Why directional mic selection for voice turns?** The mic physically closest to the speaker has the best SNR for that speaker. Selecting it at voice turn start (after wake word or button press) locks in the optimal channel for the duration of the turn. The lock happens at `mic_start` with `lock_mic: true` — not during ambient VAD activity — ensuring ambient sounds before the turn don't influence selection.

**Why mic selection rather than delay-and-sum?** At speech frequencies (<2kHz), a 72mm array has insufficient angular resolution to reliably discriminate between the 6 candidate directions. More critically, the maximum inter-mic delay is ~3.3 samples at 16kHz — requiring sub-sample fractional delay interpolation that introduces frequency-dependent phase errors causing comb filtering. Directional mic selection avoids all phase math and produces clean output.

**Frequency-domain beamforming** (implemented in `bf_capture` diagnostic tool): A frequency-domain delay-and-sum implementation exists applying exact phase shifts via FFT. Testing confirmed the approach works — flat spectral response, no interpolation artefacts. For voice pickup at typical conversational distances the SNR improvement over mic selection is marginal; mic selection remains the production path. The `bf_capture` tool is retained for future research.

**Why that result is structural, not an implementation shortfall.** Recorded 2026-07-29 after the frequency-domain result was very nearly re-proposed as a fix for far-field reach: the note above says the gain was marginal, but not *why*, and without the why it reads like something worth another go. It isn't.

Delay-and-sum improves SNR only against noise that is **spatially uncorrelated** between the mics. In a diffuse (reverberant room) field the coherence between two mics separated by *d* is `sinc(2fd/c)`, and at this aperture that stays near unity right across the speech band:

| Freq | Aperture/λ (72mm) | Coherence @36mm (adjacent) | @72mm (opposite) |
|---|---|---|---|
| 300 Hz | 0.06 | 0.99 | 0.97 |
| 500 Hz | 0.10 | 0.98 | 0.93 |
| 1 kHz | 0.21 | 0.93 | 0.73 |
| 2 kHz | 0.42 | 0.73 | 0.18 |
| 4 kHz | 0.84 | 0.18 | −0.16 |

Below ~1.5kHz — where most speech energy is — every mic is hearing between 84% and 99% the *same* noise, so there is essentially nothing for a sum to cancel. Useful decorrelation only begins around 2kHz, and the 36mm adjacent spacing puts the spatial aliasing limit at `c/2d` = **4.76kHz**. That leaves a working window of roughly 2–4.7kHz, which is exactly why the measured improvement was marginal. It is a property of the 72mm aperture, not of the algorithm or the code: no better delay-and-sum implementation changes it.

The class of algorithm that *does* extract directivity from a sub-wavelength aperture is **superdirective / differential** beamforming (and is presumably close to what the XMOS front-end in purpose-built far-field kit is doing). It is not a free upgrade: superdirective designs trade directly against **white noise gain**, amplifying uncorrelated sensor self-noise by 20dB or more at low frequencies on a 0.1λ aperture, and they need per-element magnitude and phase calibration to come anywhere near theory. Seven MEMS capsules spread across four unmatched TLV320ADC3101s, on a CPU already at ~18–20% baseline just running the mic pipeline, is not the substrate for that. Filed as a research curiosity, not a roadmap item.

**Practical consequence for far-field reach:** it is not a beamforming problem on this hardware, and not recoverable by a config change either — the ch6-vs-best-perimeter SNR difference is negligible at conversational distance (see the `beamformingEnabled` note in `em_db.py`). Reach here is set by the room's noise floor, distance and placement. The 2026-07-29 utterance analysis measured 8.7dB of noise-floor drift between two runs of the *same phrase* at ~1.3m, enough on its own to flip the transcript. The levers that genuinely exist are single-channel: `nsAsr`, wake model choice, and moving the device.

**How Amazon does it:** Amazon's `amazon.speech.sim` reads the same raw 9-channel array via Android AudioRecord and does software processing. There is no hardware beamforming output channel. The MediaTek MAGI Conference DOA feature (in `audio.primary.mt8163.so`) is designed for phone call use cases and is not active in voice assistant mode on this device.

---

## Mic Array — What Actually Happens at Each Stage

This describes the pipeline as of v2.9.4 (2026-07-18; originally written for v2.7.1): the wake stream is **ungated and AGC-free** — the device streams continuously, and all adaptation lives controller-side as measurement. The only gain in the path is the fixed 24-bit mic gain (v2.7.1). Device-side NS is gone entirely (RNNoise removed 2026-07-12; noise suppression is now controller-side DTLN on the speech-to-text stream only, per-device `nsAsr` flag), and speexdsp AEC (v2.7.3+) sits in the mono path when enabled. One cadence correction to the numbers below: GoTinyAlsa delivers whole ALSA buffers, so the mic loop actually runs on **160ms batches of 2560 frames** (69120 raw bytes), not single 512-frame periods. The stages are in order from hardware to HA.

Why the gate came out (2026-07-06 rework): the VAD gate's absolute RMS threshold is wrong in at least one room of every home, openwakeword is a streaming model that scores best on continuous audio (gated bursts spliced together measurably depress scores even with preroll), and the AGC's persistent gain state on a never-restarting stream rebaselined itself to each room's noise floor — the "wake word degrades over days, reboot fixes it" disease. Bandwidth was the reason for the gate and it doesn't survive arithmetic: 16kHz mono S16 is 32KB/s per device, 6× smaller than the TTS playback stream.

### Idle — waiting for wake word

```
ALSA card 0 device 24 (9ch S24_3LE 16kHz)
  → pcm_microphone.go subscriber channel (raw 13824-byte periods at ~31ms intervals)
  → beamformer.Process(raw, beamAngle, gain)
      — unlocked (idle): always returns ch6 (centre/omni mic)
      — smoothers still update every period (baseline stays warm;
        energy ratios are gain-invariant)
      — fixed mic gain (micGainDb, default +24dB) applied to the FULL
        24-bit sample during S16 extraction (v2.7.1) — the old path took
        the upper 2 bytes and threw away the low byte, where nearly all
        of the signal lives at this hardware's capture levels (speech
        ≈ −70dBFS raw). Clipped samples are counted and reported.
      — returns mono S16_LE 512 samples
  → vadPeriodRMS(mono) — computed for the periodic diagnostic log only
      (every ~10min, or within ~16s of a clipped sample — v2.7.1);
      does NOT gate sending on this stream (v2.7.0)
  → aec.Process(mono) — speexdsp echo cancel against the speaker's own
      output (v2.7.3; no-op while aecEnabled=false; ~14dB when converged)
  → AGC: NEVER on the wake stream (v2.7.0 — forced off regardless of config;
      adaptive gain state on a permanent stream is a rebaselining mechanism
      by construction). agcEnabled config now applies to lock_mic turn
      streams only.
  → EVERY period sent — batched into 80ms chunks, ~12.5 frames/s, 32KB/s:
      frame: [0x01][seq_hi][seq_lo][2560 bytes PCM = 80ms]
      No VAD gate, no preroll ring, no 0x04/0x05 sentinels on this stream.

Controller handle_data():
  → oww_paused.is_set()? → voice_queue (during a turn)
  → else → mic_queue (during idle)

wake_word_listener():
  → pulls from mic_queue (10s of silence now means the stream DIED —
    hardware mute still produces zero-filled frames — so the controller
    logs a warning and sends a defensive mic_start, skipped mid-turn)
  → accumulates into 80ms chunks
  → per-chunk RMS updates device.noise_floor (v2.7.0): asymmetric EWMA,
    follows drops fast (α=0.3), rises slowly (α=0.008 ≈ 10s) so speech
    can't drag it up. Measurement only — the audio is never modified.
  → OWW inference (hey_rhasspy_v0.1, threshold 0.30)
  → scores > 0.05 counted as near-misses: INFO log (rate-limited 1/2s,
    now includes rms= and floor=) + dashboard counter
  → score >= threshold → wake detected
```

**Key: the stream runs continuously and is completely stateless — no gate, no adaptive gain, nothing that can drift with room history. OWW always sees uninterrupted audio. ch6 omni during idle. Per-room adaptation happens controller-side as a noise-floor *measurement*, consumed by endpointing — never applied to the signal.**

### Wake word detected → command capture

```
wake_word_listener():
  → oww_paused.set() — routing flips: handle_data() now sends to voice_queue
  → model.reset(), buf.clear()
  → beam_lock control message (v2.7.0) — device locks the beamformer onto
    the perimeter mic with the best speech onset ratio, mid-stream, no
    restart. Sent at detection because that's the freshest onset signal the
    selector will get (though see the beamforming caveat in the table below
    — controller-side detection latency means even this is 300–500ms after
    the wake word started). beam_unlock is sent after the turn completes.
  → _run_voice_locked(device, trigger_label="wakeword(score)")
      → [esphome path] trigger_voice_turn()
          → TurnTrace created (t0 = now)
          → satellite.run_esphome_voice_turn()
              → VoiceAssistantRequest(start=True) → HA Assist pipeline opens
              → _stream_mic_audio() starts reading from voice_queue
                (whole phase wrapped in a 20s hard cap, v2.6.5 C1):
                  → first 3 frames discarded (VOICE_PREROLL_DISCARD=3, 240ms)
                    — removes wake-word tail ("...Jarvis") from audio.
                    WAKE TURNS ONLY (v2.6.5 C3): button and continuation
                    turns pass preroll_discard=0 — they have no wake-word
                    tail, discarding real audio clipped their first word
                  → controller-side 5s no-speech timeout armed
                  → timeout disarms on SPEECH, not on the first frame
                    (v2.7.0 — frames now flow continuously, silence included,
                    so "a frame arrived" means nothing). Speech = chunk RMS ≥
                    max(3 × device.noise_floor, 0.004), OR HA's own
                    STT_VAD_START event (covers quiet speech in a noisy room).
                    Wake-then-silence closes quietly at 5s again instead of
                    sitting through HA's ~10s STT timeout + error cleanup.
                  → frames sent as VoiceAssistantAudio chunks to HA
                  → stream ends on WHICHEVER ARRIVES FIRST:
                      — HA's own VAD end (_ha_vad_end, set on STT_VAD_END or
                        ERROR) — the endpointing authority; noise-robust,
                        model-driven (v2.6.5 C1)
                      — device VAD sentinel (0x04) — only exists on lock_mic
                        (button) streams now; never arrives on wake turns
                    → VoiceAssistantAudio(end=True), t_vad_end logged

NOTE: the stream never stops. No mic_stop, no mic_start_turn on OWW path.
The only changes at wake are the oww_paused flag flipping the queue routing
and the beam_lock switching the mic channel. Command audio flows in with
zero gap.
```

### HA pipeline → response

```
HA Assist:
  → STT (Whisper) → intent resolution → TTS generation
  → VoiceAssistantEvent stream: RUN_START → STT_START → STT_END →
    INTENT_END → TTS_START → TTS_END → RUN_END

Controller satellite:
  → INTENT_END received → tts_event armed (prevents premature RUN_END close)
  → TTS_URL received → t_tts_url_ms logged
  → fetch TTS from HA (48kHz mono FLAC when HA honours our declared
    supported_formats) → ffmpeg decode → 48kHz mono S16_LE PCM (v2.9.4;
    was 22050Hz + numpy resample)
  → t_tts_fetched_ms, tts_bytes logged
  → EQ → bass guard → limiter at 48kHz (mono end-to-end — no resample,
    no stereo). Guard before limiter: limiting first spends reduction
    on bass about to be discarded. See controller/CLAUDE.md, "The output
    chain"
  → mic_stop → device stream stops BEFORE playback starts (v2.6.5 —
    previously only in the post-turn finally, so the device processed
    63–65 frames of its own TTS echo per turn, contended the Wi-Fi radio
    against the incoming speaker frames, and crushed AGC gain)
  → stream PCM to device ALSA as 0x02 binary frames, 0x03 EOS
  → sleep for audio duration (acoustic feedback prevention)
  → EITHER (continuation, v2.6.5 C2): HA set continue_conversation →
    mic_start (no lock_mic) → loop into next turn with preroll_discard=0.
    The restarted stream is ungated so audio flows immediately; the
    controller sends beam_lock again the moment the user's answer clears
    the noise floor (the TTS mic restart reset the beam to ch6 omni)
  → OR (normal end): voice_queue drained WHILE oww_paused is still set
    (v2.6.5 regression fix — draining after the routing flip left stale
    ambient frames to arrive as preamble on the next turn)
  → oww_paused.clear() → routing returns to mic_queue
  → mic_start (no lock_mic) → stream restarts on ch6 omni
  → beam_unlock sent (belt-and-braces — matters for no-TTS turns where the
    stream never restarted and a beam lock would otherwise persist into
    idle wake listening)
  → stale frames drained (belt-and-braces no-op now), OWW model reset
  → [TURN] log line emitted with full timing breakdown
```

NOTE the stop/start pair around TTS is safe as of v2.7.0: streamMic's exit
path has an ownership check (d.micStopCh == stopCh) so a draining old
goroutine can't clear micActive over its replacement. Before the fix, that
race let a mic_start spawn a second concurrent stream that no mic_stop could
reach — leaked gated streams were silent while idle but duplicated every
utterance 2× (STT saw "turn on on the on the office…") and their 0x04
sentinels cleared the OWW buffer, progressively killing wake detection until
the process restarted. This was almost certainly the historical
"wake word degrades over days, reboot fixes it" bug.

### Button-triggered turn (differs from OWW path)

```
Button press (clickType=138):
  → oww_paused.set()
  → mic_stop → device stream stops
  → mic_start(lock_mic:true) → new stream with lockMic=true
      → beam.Lock(beamformingEnabled) called
        — beamformingEnabled=true: selects perimeter mic with highest onset ratio
        — beamformingEnabled=false: Lock() no-ops, stays on ch6
      → [beam] locked to chX (Y°) onset_ratio=Z logged
  → _run_voice_locked(device, trigger_label="button")
  → [same HA pipeline as above]
  → mic_stop → mic_start (no lock_mic) → back to ch6 omni
    (explicit stop first, v2.7.0: on no-TTS outcomes — cancel, error,
    no-speech — the lock_mic stream is still running and a bare mic_start
    would no-op against it, leaving the GATED, beam-locked turn stream as
    the permanent wake stream)

Button path retains stop/start because: (a) no dead zone cost — button is
pressed before speech starts, (b) the lock_mic stream is the only place the
VAD gate, preroll ring, sentinels, and (config-gated) AGC still exist.
```

### What's currently off and why

| Stage | State | Reason |
|---|---|---|
| RNNoise NS | **REMOVED** (2026-07-12) | Was calibrated for 48kHz, fed 16kHz — miscalibrated speech probability, degraded HF consonants. P0-3 resolved exactly as predicted here: deleted device-side, replaced by controller-side DTLN (`em_ns.py`, 16kHz-native) applied to the speech-to-text stream only, per-device `nsAsr` flag, default off. Wake stream stays raw. |
| AGC | **OFF on the wake stream, permanently** (v2.7.0 — ignores config). Config-gated on lock_mic turns only. | v2.6.5 re-enabled it after the echo fixes, but ResetAGC only runs at stream start and the wake stream never restarts — in any room with steady noise above vadThreshold, the release path walked gain up toward amplifying the noise floor (the RNNoise interlock that was meant to prevent this is dead while NS is off), then the fast attack compressed the wake word's envelope mid-utterance. Adaptive gain state on a permanent stream = rebaselining by construction. The fixed gain staging that replaced it shipped in v2.7.1: `micGainDb` (+24dB default) applied to the full 24-bit sample pre-truncation. |
| VAD gate (wake stream) | **REMOVED** (v2.7.0) | Absolute RMS threshold can't be right in every room; OWW wants continuous audio; the gate held open by ambient noise was also what let the AGC release run continuously. Still exists on lock_mic (button) streams for endpointing. |
| Beamforming | ON in config, **lock-back selection (v2.7.2)** | Lock is commanded at wake detection (v2.7.0, beam_lock mid-stream); detection lands 300–500ms after the wake word ends, so live onset ratios had decayed and selection was known-poor. Fixed via lock-back: a ~2s ring of per-direction period energies (frozen while locked, like the baseline); Lock() scores each direction by its top-8-period burst within the window relative to its baseline, so it selects on the recorded wake word rather than the decayed present. Unit-tested (TV-vs-decayed-speaker scenario in `beamformer_test.go`). Known caveat: TTS echo enters the ring between turns — the baseline absorbs the same energy, damping its ratio, but continuation-turn locks are the weaker case until AEC. Validate direction LED against speaker position after OTA. |
| owwSpeexNs | OFF | Available (v2.6.5, Q1): openwakeword's speexdsp suppressor, wake path only. Off by default — flip on the lounge device and A/B wake rate with TV on before fleet-wide enable. |
| Noise floor tracking | **ON** (v2.7.0, controller) | Per-device asymmetric EWMA over the continuous wake stream. Measurement only. Consumed by the SNR-relative no-speech timeout; logged as floor= in OWW lines. |

### VAD threshold guidance

**Units (v2.7.1):** all values below are *pre-gain* — measured before the fixed `micGainDb` stage. The device scales `vadThreshold` by the linear gain internally, so the config value keeps these units regardless of the gain setting; the `rms=`/`floor=` values in controller logs are *post-gain* (multiply this table by ~16 at the default +24dB to compare).

Measured signal levels at 16kHz on ch6, MICPGA=40, digital gain=88:

| Condition | Typical RMS |
|---|---|
| Dead silence (quiet room) | 0.00017–0.00019 |
| Ambient room noise | 0.00020–0.00050 |
| Conversational speech at 1.3m | 0.0004–0.0010 |
| Raised voice at 1.3m | 0.004–0.010 |

vadThreshold 0.001 sits comfortably between ambient and speech. Raise to 0.003–0.005 in noisy rooms (TV on). Dashboard slider now goes down to 0.0001 for quiet environments.

**Scope change (v2.7.0):** vadThreshold/vadSpeechMs/vadSilenceMs apply only to lock_mic (button) turn streams now — the wake stream is ungated and ignores all three. Wake-turn endpointing is HA's VAD; accidental-wake cutoff is the controller's 5s SNR-relative timeout against the measured per-room noise floor (no per-room tuning needed). The fixed-gain bump this table originally motivated shipped in v2.7.1 (`micGainDb`).

---

## Voice Pipeline

Home Assistant's Assist pipeline (via the impersonated ESPHome satellite) is
the only voice backend — the legacy claracore WebSocket path this section
used to open with was removed 2026-07-12.

```
"Hey Jarvis"
    → [same on-device path through OWW detection]
    → controller: VoiceAssistantRequest(start=True, flags=0) → HA Assist
    → mic audio streamed as VoiceAssistantAudio chunks to HA
      (wake turns drop the first 240ms of wake-word tail; button and
      continuation turns don't — v2.6.5 C3)
    → VAD end → VoiceAssistantAudio(end=True)
      — HA's STT_VAD_END is the endpointing authority (v2.6.5 C1); the
        device's own RMS-gate 0x04 sentinel is advisory and ends the
        stream only if it arrives first. 20s hard cap as backstop.
    → HA: STT (Whisper) → intent → TTS
    → HA: VoiceAssistantAnnounceRequest(media_id=url, text="...")
    → controller: mic_stop (acoustic-feedback guard, v2.6.5; skipped when
      barge-in is enabled — AEC keeps the live mic usable during playback)
    → controller: fetch TTS (one retry on transient failure; the satellite
      declares supported_formats 48kHz/mono/FLAC so recent HA transcodes at
      source) → ffmpeg decode straight to 48kHz mono S16_LE (cap 15s)
    → controller: EQ → bass guard → limiter at 48kHz (no resample step
      since v2.9.4) → stream to device ALSA as mono 0x02 frames
    → controller: MediaPlayerState ANNOUNCING → AnnounceFinished → IDLE
    → if HA set continue_conversation: mic_start → next turn immediately
      (preroll_discard=0), no wake word needed (v2.6.5 C2)
    → else: LED off, voice_queue drained, mic restart
```

No-speech branch (device's 0x05 sentinel — see WebSocket Protocol below):
```
"Hey Jarvis" → [silence for 5s, nothing said]
    → device: 0x05 (no-speech timeout) instead of 0x04
    → controller: empty VoiceAssistantAudio(end=True) sent to close HA's
      already-open pipeline cleanly, but the 30s wait for a TTS response
      is skipped entirely — no HA round-trip result is awaited
    → turn ends quietly, mic restart
```

A tap on the action button triggers the same pipeline directly, bypassing wake word detection. A second tap cancels at any stage. A *hold* (≥`BUTTON_HOLD_MS`) is a separate gesture that fires an HA event instead of starting a turn, and is measured on the device.

Under `buttonSingleTapEvent` the tap becomes an HA event too, and the button stops starting turns entirely; `buttonMultiTapMs` groups taps into single/double/triple at the cost of delaying every tap by that window. The hold's timing is measured on the device precisely because control-plane RTT here is jittery (26.4% of probes over 200ms on one device, max 9255ms); the multi-tap window is not, which is why it needs ≥350ms to be reliable — see issue #115.

---

## On-device wake word

The Echo can run the wake model itself. `owwOnDevice` is `off` (default),
`shadow` or `on`. An unrecognised value normalises to `off` at **both** ends
rather than being guessed at — neither end may assume the other is the careful
one, and the two plausible guesses ("score silently" and "start triggering")
differ by a live behaviour change.

`on` is gated on the **`oww_trigger` capability, separate from `oww_shadow`**.
Shadow shipped first, so firmware exists that scores and reports without being
able to act on it; offering those `on` produces a device that scores perfectly
and never answers. Absent the capability, `on` degrades to `shadow` — never to
`on`, which would leave the controller waiting for wakes the firmware cannot
send while no longer acting on its own.

### Shadow — score and report

**Shadow mode scores and reports; it never acts.** It exists to answer whether
on-device detection is good enough to trust, by running both detectors over the
same audio. The tap sits where the ungated wake stream's frames are written to
the wire, so the device scores byte-identical 80ms frames on identical
boundaries — a score difference can then only be the engine, not the framing.

- Inference runs on **its own goroutine**, never the mic goroutine: it costs
  ~31ms per 80ms frame against a mic loop reading 160ms ALSA batches into a
  ring only 160ms deep. `Push` hands off to a buffered channel and returns,
  and the scorer **drops frames and counts them** when behind. A run that
  drops frames is informative; one that stutters the microphone is not.
- **Nothing is sent per frame.** Threshold crossings go immediately (rare, and
  their whole value is the timing); everything else is a window summary riding
  the existing ~30s stats tick.
- **The device never sends a timestamp** — an Echo's clock is bogus pre-NTP, so
  it reports how long *ago* a crossing happened and the controller converts
  against its own monotonic clock.
- **Thresholds must match or the comparison is meaningless.** The controller
  drops its bar to `bargeInThreshold` while the speaker streams, so the device
  mirrors that and never *raises* the bar if misconfigured above the normal one.

Correlation happens at turn-persist time, not at detection — a crossing report
can land after the wake it belongs to. The nearest crossing within a 2.0s
window wins and is consumed, so two quick turns can't both be credited to one
crossing.

### On — the device triggers

The device sends `oww_wake` (score, the threshold it actually cleared, and how
long *ago*) instead of a crossing report. It lands in a one-slot holder and the
wake listener acts on it on its next mic frame, ~80ms, because that is where
turn setup lives — capture routing, beam lock and arbitration have to happen
together, and driving them from the control-plane handler would be a second
copy of the most delicate sequence in the controller.

What this buys is **latency and resilience, not bandwidth**: the mic stream is
unchanged, because the controller still runs the turn. What changes is that the
wake decision no longer crosses a network with measured 1.1–2.6s idle RTT
excursions, and a controller restart no longer deafens the device.

- **The controller keeps scoring, and stops triggering.** Its score records
  whether it agreed (`turns.ctrl_wake_score`) — the same comparison with the
  roles inverted, and the only place a *controller* miss is visible. Without
  it, turning a device on would silently end the measurement: a device score
  with nothing beside it reads as perfect agreement rather than as no data. It
  is also what leaves barge-in untouched, since barge is scored
  controller-side over the turn's own audio.
- **The recorded wake instant is the crossing, not the arrival.** Using arrival
  folds the network hop into every comparison and every arbitration decision.
- **Mute is checked on the device** as well as controller-side. The `mic_start`
  refusal and the hardware ADC mute already make a muted wake harmless, but
  harmless still means the ring lights and HA runs a pipeline. The crossing is
  still reported — it is real data about the detector.
- **A pending wake expires after 4s**, measured from the crossing. That stale
  means the person has finished speaking, so acting on it answers into silence.
  The expiry logs the age, which is the instrument for whether the trigger
  needs more slack.

Two known gaps, both recorded rather than hidden:

- **Arbitration is not corrected for this.** Claims are still compared by
  arrival, so on a multi-device fleet a device can lose its window because its
  claim was late and the wrong room answers. Solo fleets skip the window
  entirely. The fix needs RTT-corrected claim times, never revoking a granted
  claim, and a window held longer than it is measured.
- **A NULL `ctrl_wake_score` is ambiguous.** It means the controller did not
  detect the utterance *or* never got to look — if it had not yet scored that
  audio when the turn started and drained the queue, there is no second
  chance. Answering that needs the unscored backlog buffered and scored after
  the turn.

ONNX Runtime plus three models must be installed at
`/data/local/share/echomuse/oww` — they are **not** in the firmware (12.3MB
would double the OTA payload and both A/B slots). The provisioning wizard
pushes them over USB/ADB; fielded devices get them over the shell plane from
the Updates tab. Absence is an ordinary condition, logged once, and the device
carries on with controller-side wake word. Costs ~38% of one core permanently
on top of the ~18–20% mic-pipeline baseline, so **enable it one device at a
time.**

---

## WebSocket Protocol

### Control plane (`ws://server:8767/control`) — JSON

Device → Server:
```json
{"type": "register", "device_id": "G0K0XXXXXXXX", "ip": "...", "version": "v2.3.0", "capabilities": [...]}
{"type": "button", "clickType": 138, "down": false, "heldMs": 812}
{"type": "mute_state", "muted": true}
{"type": "volume_state", "volume": 40}
{"type": "log", "level": "info", "message": "..."}
{"type": "stats", "cpuPct": 25.5, "coresOnline": 2, ...}   // ~30s; carries the
                                                            // RTT and shadow
                                                            // window summaries
{"type": "playback_stats", "periods": 56, "underruns": 0, "minDepth": 41, ...}
{"type": "ambient_light", "lux": 132}      // cap: ambient_light; omitted, never 0
{"type": "oww_shadow_cross", "score": 0.71, "agoMs": 340}  // cap: oww_shadow
{"type": "ble_adverts", "adverts": [...]}  // bleProxyEnabled
{"type": "identify"}
{"type": "wifi_result", ...} / {"type": "wifi_scan_result", ...}
{"type": "pong", "seq": 41}                // seq echoed; unsolicited keepalive
                                           // pongs carry none and are ignored
```

`capabilities` is what negotiation keys off — **never the version string.**
Currently: `mic`, `speaker`, `leds`, `led_anim`, `buttons`, `oww_shadow`,
`button_hold`, `audio_mix`, and `ambient_light` (the last only when the sensor
actually reads).

Server → Device:
```json
{"type": "ack", "device_id": "G0K0XXXXXXXX"}
{"type": "pending"}
{"type": "config", "adcDigitalGain": 88, "adcMicpga": 40, "vadThreshold": 0.001, ...}
{"type": "leds", "leds": [{"id": 0, "r": 0, "g": 180, "b": 0}, ...]}
{"type": "mic_start"}
{"type": "mic_start", "lock_mic": true}
{"type": "mic_stop"}
{"type": "beam_lock"}      // v2.7.0: lock beamformer onto best perimeter mic
                           // mid-stream, no restart (no-op if beamforming
                           // disabled in config or already locked)
{"type": "beam_unlock"}    // v2.7.0: release beam lock, back to ch6 omni
{"type": "led_anim", "pattern": "spin", "colors": [...], "periodMs": 900, "ttlSec": 135}
                           // v2.9: device renders frames on its own ticker;
                           // ttlSec is a dead-man so a dropped clear cannot
                           // leave the ring lit. cap: led_anim
{"type": "duck", "db": -18.0}      // v2.10.0, cap: audio_mix
{"type": "speaker_flush"}          // voice plane — barge-in, turn cancel
{"type": "music_flush"}            // music plane — genuine stop/pause
{"type": "volume_set", "volume": 40}
{"type": "wifi_change"} / {"type": "wifi_commit"} / {"type": "wifi_scan"}
{"type": "shell_open"}
{"type": "shell_close"}
{"type": "ping", "seq": 41}        // every 5s; seq makes the RTT measurable
                                   // against one monotonic clock
```

### Data plane (`ws://server:8767/data`) — binary

Device → Server (mic frames):
```
[0x01][seq_hi][seq_lo][mono S16_LE PCM, 2560 bytes = 80ms]  — audio (continuous on the wake stream since v2.7.0; VAD-gated speech on lock_mic streams)
[0x01][seq_hi][seq_lo][0x04]                                 — VAD end (lock_mic streams only since v2.7.0)
[0x01][seq_hi][seq_lo][0x05]                                 — no-speech timeout (lock_mic streams only; see below)
```
All three share the same `frameTypeMic` (`0x01`) wrapper and seq header — the VAD sentinels are single-byte *payloads*, not distinct top-level frame types. (0x02/0x03 below are genuinely distinct top-level types, speaker-direction only, no seq header — don't confuse the two framing conventions.)

**No-speech timeout (0x05), added v2.6.0.** `streamMic` (device/internal/client/data.go) only arms this when `lock_mic: true` was set on the `mic_start` that began the stream — i.e. only for a bounded voice turn (post-wake-word or button press), never for the permanent `lock_mic`-absent OWW listening stream. If no speech is ever detected (RMS never crosses `VadThreshold` for `VadSpeechMs` consecutive periods) within 5s of turn start, the device gives up locally and sends `0x05` instead of waiting on the existing silence-after-speech hysteresis, which never engages if speech never started. Distinguishing 0x05 from 0x04 lets the controller skip contacting HA's Assist pipeline entirely for a turn that never had anything to transcribe — mirrors Alexa's behaviour of quietly giving up on "wake word, then silence" rather than round-tripping to the backend just to receive `stt-no-text-recognized`. **This must never be armed for `lock_mic`-absent streams** — an earlier build armed it unconditionally, which silently killed the permanent wake-word listening stream 5s after every boot/reconnect with nothing to restart it (wake word "stopped working entirely," diagnosed via device log showing repeated `no speech detected within timeout` firing exactly 5s after every idle `Mic streaming started`, with no corresponding `mic_start` to revive it). Since v2.7.0 the failure mode is doubly covered: the `lock_mic`-absent stream has no VAD machinery at all, and the controller detects a dead wake stream (10s without frames) and sends a defensive `mic_start`.

Server → Device (speaker frames):
```
[0x02][mono S16_LE PCM, 4096 bytes = one ALSA period]   — voice: TTS
                                                           responses and
                                                           announcements.
                                                           Mono on the wire
                                                           since v2.8.4; the
                                                           device duplicates
                                                           L=R at the ALSA
                                                           write
[0x03] end of voice stream
[0x04][mono S16_LE PCM, 4096 bytes = one ALSA period]   — music (v2.10.0,
                                                           `audio_mix`)
[0x05] end of music stream
```

**0x04/0x05 mean different things in each direction — this is deliberate but
easy to misread.** Device→server they are single-byte *payloads* inside a
`0x01` mic frame (VAD end / no-speech timeout, above); server→device they are
genuine top-level frame types carrying music. The two never appear on the
same leg of the link, so there is no ambiguity on the wire, but a reader
scanning for `0x04` will find both.

**The music plane (v2.10.0).** Music rides its own frame types into a second
buffer on the device, and the two buffers are **mixed at the ALSA write** —
music is attenuated by `duckDb` (default −18dB) while a voice stream is
active, voice never is. Ducking has to happen device-side: `LEAD_S` = 4.0s
means the next four seconds of music are already in the device's buffer when
a wake word fires, so audio that has left the controller cannot be ducked by
the controller. The gain ramp is a constant slew interpolated **per sample**,
not per period — a gain step at a period boundary is an audible click landing
on exactly the moment the user started speaking.

Firmware without `audio_mix` never receives 0x04 at all; the controller falls
back to pausing music for the turn. Correspondingly there are two flush
messages and they are not interchangeable: `music_flush` clears the music
plane (a genuine stop/pause), `speaker_flush` clears the voice plane
(barge-in, turn cancel). A voice turn sends **neither** — flushing discards
the buffered audio that makes ducking instant, and on a non-seekable stream
it cannot be recovered. Both planes honour the same discard-until-EOS
contract, and a flushed stream must not leave EOS armed for the next one (the
regression that made a following response report itself complete at its first
buffer dip).

### Shell plane (`ws://server:8767/shell/{device_id}`) — binary

Demand-opened by the Go binary dialling **outbound** to the controller on receipt of a `shell_open` control message. Single session enforced. The controller proxies this connection to the dashboard terminal. No inbound ports on the device.

Two modes (v2.7.1):

- **PTY** (`shell_open` with `pty: true` — dashboard sessions): the device attaches `/system/bin/sh` to a real pseudo-terminal (`/dev/ptmx`, `TERM=xterm-256color`, new session with controlling TTY), giving an interactive mksh with prompt, line editing, job control, and full-screen apps. The device signals the established mode by dialling `/shell/{device_id}?pty=1`; the controller relays it to the dashboard as a `shell_meta` text message before any bytes flow. Input from the dashboard is framed binary: `0x00` + stdin bytes, or `0x01` + cols/rows (uint16 BE each) for resize (`TIOCSWINSZ`). Output is raw. If PTY allocation fails, the device falls back to the pipe and omits the query flag.
- **Pipe** (`pty` absent — programmatic sessions: OTA transfer, `_shell_run`): raw unframed stdin/stdout, no echo, no prompt — exactly what the output-parsing callers need. Unchanged from earlier versions.

The controller proxies bytes verbatim in both modes; the framing is interpreted only at the endpoints.

---

## Connection Lifecycle

```
Device boots
  → orange LED pulse (searching for server)
  → mDNS browse: _emcontroller._tcp.local (grandcat/zeroconf)
  → credentials at /data/local/etc/echomuse/ + tls_port TXT property?
      → dial wss://:8770 with pinned CA + X-EM-Token (v2.9.3)
      → else plain ws://:8767 (rollout fallback until REQUIRE_DEVICE_TLS=1)
  → connect /control → register (device_id = ro.serialno, version)

  CASE: unknown device, strict mode
    → server: sends {"type": "pending"}
    → device: slow white LED pulse — waiting for approval
    → device retries every 30s

  CASE: approved device
    → server: sends {"type": "ack"} + {"type": "config"}
    → device: applies config (tinymix for hardware params)
    → LEDs off (connected)
    → connect /data → identify
    → server: mic_start sent (no lock_mic — OWW mode)
    → device: mic streaming started on ch6 (centre/omni)
    → OWW listening (device shows IDLE state in dashboard)
```

If control drops → data cancelled → orange pulse resumes → both reconnect together on next mDNS discovery.
Controller detects dead connections within 30s via WebSocket protocol keepalives (ping 20s, timeout 10s).

---

## Key Files to Keep Safe

| File | Purpose |
|------|---------|
| `boot_patched.img` | SELinux-patched boot image |
| `magisk.db` | Pre-seeded root grant database |
| `Magisk-v17.3.zip` | Magisk installer |
| `f1r30s.zip` | ADB enablement patch |
| `update-kindle-csm_biscuit-272.6.8.0_user_680767620.bin` | FireOS 5 firmware |
| `server` | Compiled EchoMuse binary (ARM, API 22) — or fetch from GitHub releases |

If you need to reflash: Steps 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9. Your saved `boot_patched.img` already contains the SELinux patch — no need to repatch from scratch.

---

## Troubleshooting

### Device not connecting to server

```bash
adb shell su -c 'cat /tmp/server.log'
```

Common causes:
- **`mDNS: no server found`** — server not advertising. Check `dns-sd -B _emcontroller._tcp local` from Mac — should show `echomuse`.
- **White pulse, not orange** — device found the controller but hasn't been approved yet. Log into the management dashboard and approve the device.
- **`Connection lost: unexpected EOF`** — connecting to wrong server (stale mDNS cache). Another device on network may be advertising `_emcontroller._tcp`. Check `dns-sd -B _emcontroller._tcp local` from Mac.
- **p2p0 interference** — check `ip link show p2p0` on device — should be DOWN.

### No audio

```bash
adb shell su -c 'tinyplay /data/local/tmp/test48s.wav -D 0 -d 23 -p 2048 -n 4'
```

If this hangs: mixer not initialised. Run the tinymix commands from Step 9 manually.

### Mic not working / wake word not triggering

Check mic gain:
```bash
adb shell su -c 'tinymix -D 0 89'  # should be 88
adb shell su -c 'tinymix -D 0 92'  # should be 40
```

Wake word detection uses ch6 (centre/omni). VAD threshold defaults to 0.001 normalised RMS — adjustable via config push from the dashboard. In noisy environments, raise to 0.003–0.005.

Check OWW model is loaded in controller logs — should see `OpenWakeWord model ready` on device connect.

### ADB not available after boot

```bash
adb shell su -c 'setprop persist.service.adb.enable 1'
adb shell su -c 'setprop persist.sys.usb.config mtp,adb'
adb shell su -c 'start adbd'
```

### Monitor active PCM devices

To see which processes own active ALSA devices in real time:

```bash
adb push pcm_watch.sh /data/local/tmp/pcm_watch.sh
adb shell su -c 'chmod 755 /data/local/tmp/pcm_watch.sh && /data/local/tmp/pcm_watch.sh'
```

`pcm_watch.sh`:
```sh
#!/system/bin/sh
while true; do
    for f in /proc/asound/card0/pcm*/sub0/status; do
        line=$(grep "owner_pid" "$f" 2>/dev/null)
        if [ -n "$line" ]; then
            pid=${line##*: }
            name=$(cat /proc/$pid/comm 2>/dev/null)
            state=$(grep "^state:" "$f")
            state=${state##*: }
            echo "$f pid=$pid state=$state -> $name"
        fi
    done
    sleep 2
done
```

---

## Audio Notes

**Why device 23?** The biscuit exposes 25+ PCM devices. Device 23 is the TLV320 DAC output path. Most other devices are modem/voice paths or internal DSP routes that hang or error on open.

**Why keep echoaudioservice?** The MediaTek audio DSP requires initialisation that happens inside Amazon's audio HAL (`audio.primary.mt8163.so`). Without `echoaudioservice` running, the I2S clock never starts and `tinyplay` hangs indefinitely. The service is a manifest stub — no Java code — its sole job is to trigger HAL initialisation via the Android audio framework.

**`stop media`, and why the ordering matters.** Since the audio-jack fix (#80), `PcmSpeaker.Init()` also runs `stop media` before opening the PCM. It has to: with a headphone plug inserted at boot, mediaserver claims device 23 and our blocking `snd_pcm_open` waits behind it forever, taking the whole device down with it (no buttons, no wake word, no registration — everything in `main()` is initialised after the speaker).

This depends on the HAL having already initialised by the time we stop it, per the note above. On measured boots it has: mediaserver brings the audio path up at ~15s (visible in dmesg as its `AudioOut_2` thread running open/hw_params/prepare on the codec) and our server starts at ~21s. So the sequence is HAL init → we stop it → we take the device, every boot, in that order. Verified on hardware with and without a plug inserted.

**`stop media` is temporary, and that is fine — better than fine.** Android restarts mediaserver shortly afterwards: measured on hardware, `init.svc.media` reads `running` again with a live pid, while our server still owns `pcm23p` in `RUNNING` state. So `stop media` is a "get out of the way for a moment" rather than a removal, and what actually makes the fix work is winning the device once and then holding it for the life of the process. `Init()` re-runs it on every start, so an OTA or supervisor restart gets the same treatment.

That also disposes of the worry above: because mediaserver comes back, the HAL, the DSP and the I2S clock stay initialised. Nothing is being permanently deprived. `echoaudioservice` is untouched and still required. **Do not turn this into a permanent disable** — that would remove the very thing the first note says the audio path depends on.

One consequence worth knowing: with mediaserver alive, Android still reacts to jack events. Inserting a plug makes its `AudioOut_2` thread reconfigure the amp, DAC mux and ramp underneath us. Removal produced no such reaction in testing — only our own `tinymix`. If codec state changes with nothing in our logs to explain it, this is why.

**The mixer defaults are wrong.** Three mixer controls must be set after every boot — `start_server.sh` handles this automatically. Without them, tinyplay hangs silently on device 23.

**The dummy mixer service is required.** EchoMuse's speaker Init() calls `stop mixer` as its first step. Without a `mixer` service in init.rc, this call fails. Adding a dummy service allows `stop mixer` to succeed.

**Amp click/hiss suppression.** Order matters (found on hardware, 2026-07-10): `pcm_speaker.go` Init() mutes the output (tinymix ctl 61 → 0), opens the PCM stream and lets the silence loop clock the DAC for ~100ms, *then* enables the amp (ctl 5 On), waits 50ms for it to settle, and unmutes last. Enabling the amp onto a floating (unclocked) DAC and unmuting before stream-open was the source of the click on every service start. Shutdown is the mirror image: on SIGTERM the server's `PcmSpeaker.Close()` mutes → amp off → closes the stream, and `start_server.sh` repeats mute + amp-off after every server exit (covering SIGKILL/panic) — an enabled amp on an idle DAC audibly hisses for as long as the server is down (worst case: between OTA slots).

**Mute implementation.** The mute button (KEY_MUTE, evdev code 113) arrives on `/dev/input/event1`. Mute sets the ADC mute controls on **all four codec chips** (tinymix ctls 105/106, 123/124, 141/142, 159/160 — chip-A-only coverage was a known gap until v2.7.4; the sibling controls were confirmed from the full `tinymix -D 0` dump in `device/tools/tinymix_controls_output.txt`), so every mic including ch6 is physically muted. The mute controller intercepts the button locally, applies the tinymix change, updates the LED ring (red = muted) and the discrete button LED (gpio444, active-high — v2.9.5; earlier firmware drove gpio445 per Amazon's HAL constant, which is off by one and muxed away, so the button never lit), and, until 2026-08-08, blocked dot button events outright. It no longer does: presses are forwarded carrying the mute state and the controller refuses only the voice turn (`em_button.decide`), so a **hold still fires its HA event while muted**. Blocking everything was correct while the dot button meant one thing and became wrong when a hold started firing an event. Mute remains device-sovereign regardless, because the guarantee is the ADC mute plus the device rejecting every `mic_start` while muted, not the button filter. Mute also stops a running mic stream (v2.6.5) and, controller-side, terminates an active voice turn (v2.7.8). Since v2.9.4 mute is **persistent**: written to `/data/local/etc/echomuse/state.json` on toggle and restored at boot before any connection — device-sovereign, so a muted Dot comes back muted with or without a controller.

**Mic gain — all four ADCs.** All four ADC pairs (A–D) are set to digital volume 88 and MICPGA 40. This matches Amazon's own initialisation values confirmed by analysing the unmodified device mixer state. Equalising all four ensures consistent sensitivity across all perimeter mics for directional selection.

**WiFi wake lock.** FireOS aggressively suspends the WiFi interface during inactivity, dropping WebSocket connections. Writing `"EchoMuse"` to `/sys/power/wake_lock` prevents this.

**Speaker streaming.** Audio is streamed as binary frames (4096 bytes = one mono ALSA period — mono on the wire since v2.8.4; the device duplicates L=R at the ALSA write) over the data plane WebSocket. The device maintains a priority channel — the silence loop yields to real audio naturally, with backpressure at ALSA playback rate (~42ms/period). TTS is 48kHz mono end-to-end since v2.9.4 (ffmpeg decodes at the wire rate; HA transcodes at source when it honours the declared supported_formats — no controller resample step). The device buffers ~5.5s and holds playback until ~1s is queued or EOS arrives (v2.8.4 — WiFi-stall protection for marginal links).

**OWW threshold.** 0.3 works well for a London/Bristol accent — the default 0.5 is calibrated for American English.

**VAD threshold.** 0.001 normalised RMS is the default (v2.6.5 — corrected from a drifted 0.003 that sat above measured conversational speech at 1.3m). Adjustable via config push from the dashboard — no rebuild required. In noisy environments (music, TV), raise to 0.003–0.005.

**VAD end signal.** When the device VAD gate closes (speech followed by `vadSilenceMs` of silence, default 900ms; button/lock_mic turns only — the wake stream is ungated), the device sends a `0x04` sentinel. The controller ends the HA audio stream if it arrives before HA's own `STT_VAD_END` — HA's VAD is the endpointing authority for wake turns; the device sentinel is what actually ends button turns. Note (v2.9.4): the gate windows now run at their configured durations — a counting bug against the mic's 160ms batch size previously made both ~5× longer than set.

**Directional mic locking — onset ratio.** When the controller sends `mic_start` with `lock_mic: true` (voice turn start), the device locks to the perimeter mic with the highest onset ratio: `energySmooth[di] / energyBaseline[di]`. This selects the direction with the biggest *recent energy increase* rather than highest absolute energy, making the lock robust to continuous background noise sources (TV, fan). Two parallel smoothers: fast (α=0.9, ~320ms) and slow (α=0.995, ~10s baseline). The slow baseline is frozen while locked. The lock is idempotent across VAD oscillation. Releases on `mic_stop`.

**Direction estimation — onset ratio.** Two parallel smoothers run per direction: fast (α=0.9, ~320ms) tracking instantaneous energy, and slow (α=0.995, ~10s) tracking the background noise floor. At lock time, the direction with the highest `energySmooth / energyBaseline` ratio is selected — this is the direction with the biggest *recent energy increase* (speech onset), not the direction with the highest absolute energy (TV, fan). The slow baseline is frozen during voice turns to prevent the speaker's own voice from corrupting the noise estimate. This reliably picks the speaker direction even with a television on in the room.

**LED direction overlay.** The direction arc is overlaid on the solid green listening ring during voice turns only (not during idle wake word listening). The overlay uses the controller-set base ring state rather than accumulating — each period resets to the base green and applies the direction marker fresh. Primary direction LED: bright light green (R:0 G:255 B:80). Adjacent LEDs: base green boosted by 60. The overlay stops immediately when the controller sends the thinking spinner (spinner LEDs are not solid green, so `listeningLEDs` flag goes false).

**LED physical mapping.** 12 LEDs (IS31FL3236A), one either side of each perimeter mic. LED 0 is physically at 240° (just clockwise of MK5 at 210°). Volume sweep confirmed: starts at LED 0, sweeps clockwise. Offset formula: `LED = ((angle - 240 + 360) % 360) / 30`.

**Audio processing pipeline.** Each 160ms mic batch of raw beamformed audio passes through: (1) speexdsp AEC (v2.7.3, when enabled) — subtracts the speaker's own output, whole mic path including the wake stream. (2) AGC (button/lock_mic turns only; never the wake stream) — targets -22dBFS RMS with fast attack (0.05) and slow release (0.005); release frozen during silence to prevent noise floor amplification. VAD decisions are made on pre-AGC audio to keep the threshold stable. Device-side RNNoise was removed 2026-07-12 — noise suppression is controller-side DTLN on the speech-to-text stream (`nsAsr` flag).

**Acoustic feedback prevention.** `stream_speaker` completes well ahead of actual playback (the WS write runs ~2× realtime and the device buffers ~5.5s). Without compensation, the mic would restart while the speaker is still playing, and the assistant would hear itself and trigger another turn. The controller sleeps for the remaining playback duration (plus the ~1s prime allowance) after streaming, racing `cancel_event` so barge-in cuts the wait instantly. With barge-in enabled the mic never stops at all — AEC is what keeps the live mic usable during playback.

**Stock's playback processing, measured off a device (2026-08-20).** Amazon's
own chain for the speaker path, from `/system/vendor/etc/audio-algorithms/`:

```
UserEQ  ->  Equalizer FIR  ->  MBCL
(taste)     (driver correction)  (excursion protection)
```

Our chain is the same order — EQ, bass guard, limiter — with the middle stage
missing entirely (#247).

`AFE.cfg` selects among six FIR files by volume (`"Volume Boundary": [50, 60,
70, 80, 90, 100]`), but all six are one curve at six gains (`EQ_100` is
`EQ_50` x 5.334838 exactly), so **tone is constant across volume and only the
level is banded**. `EQ_50.cfg` is 1024 FIR taps at 48kHz. Its response,
normalised to 0dB at 1kHz because the absolute level is that volume banding
rather than tone:

| Hz | dB | Hz | dB | Hz | dB |
|---|---|---|---|---|---|
| 100 | +20.1 | 500 | +2.7 | 2800 | **-14.7** |
| 125 | +24.8 | 700 | +0.1 | 3500 | -11.9 |
| 150 | **+26.2** | 1000 | 0.0 | 5000 | -3.5 |
| 200 | +25.6 | 1400 | -2.1 | 8000 | +3.1 |
| 250 | +19.9 | 2000 | -7.9 | 10000 | +3.1 |
| 350 | +12.3 | | | | |

**The design is coherent and the two halves belong together**: boost the
lowest region the driver *can* radiate (150-250Hz, ~+25dB), and let MBCL band
1 delete everything below 115Hz that it *cannot* (20:1 from -50dB, floor
-40dB). Boost what works, remove what does not. Implementing only the removal
half — which is what `em_mbc` is — protects the driver correctly and leaves
the output thin, because nothing puts energy back where the driver works.

Two caveats on the numbers. **Below ~100Hz they are not trustworthy**: 1024
taps at 48kHz is ~47Hz of resolution, so treat the sub-100Hz end as undefined
rather than as a measurement. And **this is a correction curve, not a driver
response** — it says what Amazon decided this speaker needed, which is strong
evidence about the driver but not the same thing. A measured driver response
is still the open item, and it needs hardware.

The table is recorded here because the same coefficients were read off a
device on 2026-08-19 and only the *conclusion* was written down, so they had
to be fetched a second time. The vendor `.cfg` files themselves are Amazon's
and are deliberately not committed; this derived response is a fact about the
hardware.

**Stock knows whether something is in the jack, and retunes for it.**
`AFE.cfg` declares `"Num Max Speakers": 2` with `"Num Internal Speakers": 1`,
`"default speaker mode": "internal"`, and keys the residual-echo suppressor's
tuning tables by that mode (only the `internal` block is populated on this
unit). The second speaker is the 3.5mm line out, and the reason the front end
cares is echo cancellation: headphones present no acoustic echo path at all,
and an external speaker presents a completely different one. **EchoMuse has no
speaker-mode concept** — our AEC assumes the internal speaker always. Worth
knowing for #117/#141 and before any line-out work.

**Turn timeouts.** The mic-streaming phase of a turn carries a 20s hard cap, and a turn where speech never starts closes locally after 5s (controller-side SNR-relative timeout on wake turns, device `0x05` sentinel on button turns) instead of round-tripping to HA for an empty transcription.

**Stale queue drain.** After each voice turn, the mic queue is drained and the OWW model is reset before wake listening resumes. This prevents the device's own speaker output (buffered during playback) from immediately triggering another wake word detection.

**OWW routing during turns.** While a turn is active (`oww_paused`), incoming mic frames route to `voice_queue` instead of the wake model. During thinking and playback the `_barge_watcher` scores that queue with its own OWW instance — a wake word spoken over the response cancels playback (barge-in, threshold deliberately below `owwThreshold` because speech-over-TTS scores are depressed ~25dB by the echo).

**mDNS library.** The `hashicorp/mdns` library fails to resolve the controller IP when python-zeroconf sends PTR responses with the A record under the hostname rather than the service name. Replaced with `grandcat/zeroconf` which is RFC 6762/6763 compliant and handles this correctly.

---

## Step 9 — Configure Audio for Speaker Playback

The ALSA mixer is initialised with incorrect defaults — the external speaker amp and DAC are disabled. Without fixing this, tinyplay will open the PCM device and hang silently. This is handled automatically by `start_server.sh`, but it's useful to understand and test independently.

### Understanding the audio hardware

The biscuit uses a MediaTek MT8163 SoC with a TLV320AIC32x4 external codec. Speaker playback goes through ALSA card 0, **device 23**, at 48kHz stereo S16_LE, period size 2048, period count 4.

The mixer has 239 controls. Three are wrong at boot:

| CTL | Name | Default | Required |
|-----|------|---------|----------|
| 5 | Ext_Speaker_Amp_Switch | Off | **On** |
| 56 | Audio_I2S1_Setting | Off | **On** |
| 64 | HP DAC Playback Switch | Off Off | **On On** |

### Test audio manually:

```bash
adb shell "su -c 'tinymix -D 0 5 On && tinymix -D 0 56 On && tinymix -D 0 64 1 1 && tinymix -D 0 61 100 100'"
```

Generate a test tone and play it:

```python
python3 - <<'EOF'
import struct, math
rate=48000; dur=2; freq=440
samples=[int(32767*math.sin(2*math.pi*freq*i/rate)) for i in range(rate*dur)]
stereo=[]
for s in samples: stereo.extend([s,s])
with open('/tmp/test48s.wav','wb') as f:
    f.write(b'RIFF')
    f.write(struct.pack('<I', 36+len(stereo)*2))
    f.write(b'WAVEfmt ')
    f.write(struct.pack('<IHHIIHH', 16, 1, 2, rate, rate*4, 4, 16))
    f.write(b'data')
    f.write(struct.pack('<I', len(stereo)*2))
    for s in stereo: f.write(struct.pack('<h', s))
print('done')
EOF
adb push /tmp/test48s.wav /data/local/tmp/test48s.wav
adb shell "su -c 'tinyplay /data/local/tmp/test48s.wav -D 0 -d 23 -p 2048 -n 4'"
```

You should hear a clean 440Hz tone.

---

## Step 10 — Server Setup

EchoMuse connects to the controller via mDNS discovery. The controller must be running and advertising before the device boots (or the device will retry with exponential backoff until it finds it).

### mDNS advertisement

The controller advertises `_emcontroller._tcp.local` on port 8767 (with a `tls_port` TXT property pointing at the wss listener on 8770 once the PKI is up). The controller container runs with `network_mode: host` so multicast reaches the LAN.

**Proxmox note:** If running in a Proxmox LXC, the bridge requires the mDNS multicast MAC to be added manually:

```bash
# On Proxmox host
ip maddr add 01:00:5e:00:00:fb dev vmbr0
# Add to /etc/network/interfaces for persistence:
# post-up ip maddr add 01:00:5e:00:00:fb dev vmbr0
```

### Verify discovery from a Mac:

```bash
dns-sd -B _emcontroller._tcp local
# Expected: clara._emcontroller._tcp appears
```
