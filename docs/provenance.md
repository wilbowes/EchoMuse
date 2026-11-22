# Method provenance

Where each signal-processing method in EchoMuse comes from: the oldest
published source we know of, and where it runs. Engineering record only; it
asserts nothing about anyone's rights.

**How to use it.** A new method gets a row in the same change that adds it.
Build from methods with an old published source where one does the job. A row
with no old source on file names the nearest older method that does, and what
switching to it costs, measured where we have the number.

Status:

- **old**: published in 2006 or earlier.
- **recent**: published after 2006 under an open licence, given in the
  source column. Date only; not a flag.
- **none on file**: we have no published source for it. The fallback column
  says what would replace it. This is the only status that asks for action.

Scope: the audio path on `main` (capture, cancellation, beam, wake, barge,
speech gate, output chain) and the turn stage in flight. Transport, TLS, OTA,
provisioning and the dashboard are not signal processing and are left out.
Last full pass: 2026-11-22.

## Capture and echo cancellation (device)

| Method | Where | Source | Status | Fallback (cost) |
|---|---|---|---|---|
| Multidelay block frequency-domain adaptive filter (MDF) | `device/internal/aec` (Speex `mdf.c`, BSD) | Soo & Pang 1990; Speex 2002– | old | — |
| MDF learning rate set from estimated leakage, for double talk | same | Valin 2007, *IEEE TASLP*; in Speex (BSD) since 2007 | recent | fixed step size with a Geigel double-talk detector (Duttweiler 1978); not measured |
| Loopback delay by normalised cross-correlation | `aec/dealias.go`, `echoref` | Knapp & Carter 1976 (GCC) | old | — |
| Decimation through an anti-alias low-pass | `aec/dealias.go` | Crochiere & Rabiner 1983 | old | — |
| Frame AGC: RMS target, fast attack, slow release gated on speech | `processor` | broadcast/telephony AGC; ITU-T G.169 (1999) | old | — |

## Direction and microphone choice (device)

| Method | Where | Source | Status | Fallback (cost) |
|---|---|---|---|---|
| Pick the mic with the largest short-term/long-term energy ratio | `beamformer` `Lock` | selection combining (Brennan 1959); STA/LTA onset (Allen 1978) | old | — |
| Look-back window over the wake word for that choice | `beamformer` `burstRatio` | as above, over a stored window | old | — |
| Fixed steering to a configured angle (nearest mic) | `beamformer` | trivial | old | — |

## Wake word (device and controller)

| Method | Where | Source | Status | Fallback (cost) |
|---|---|---|---|---|
| Log-mel features → speech embedding → small classifier | `wakeword`, controller openwakeword | Lin et al. 2020 (Google speech embedding, Apache-2.0); openWakeWord, Scripka 2022 (Apache-2.0) | recent | HMM keyword spotting (Rohlicek et al. 1989; Wilpon et al. 1990); far less accurate, not measured |
| Threshold crossing with a refractory period | `wakeword/shadow` | trivial | old | — |
| Pre-trigger ring buffer: a session starts `WakeLead` before the crossing | `listen` | pre-trigger capture (instrumentation practice) | old | — |
| Wake level logged beside capture time (nothing acts on it) | `client/wakelevel.go`, `em_wakelevel` | RMS measurement | old | — |
| Custom models trained on synthetic speech | `oww_forge` | Lin et al. 2020; openWakeWord | recent | — |

## Barge-in (device and controller)

| Method | Where | Source | Status | Fallback (cost) |
|---|---|---|---|---|
| Wake word scored on the cancelled mic during playback; playback stops when it fires | `em_barge`, `listen/barge.go` | IVR barge-in over an echo canceller (telephony practice, 1990s) | old | — |
| Lower bar during playback, two consecutive frames to fire | `em_barge` | threshold plus M-of-N debounce (detection theory) | old | — |
| **Several other mics scored during a reply; any one may fire** | `client/taps.go` (#1240; biscuit 3 taps, donut all) | product practice | **none on file; kept by decision (2026-11-22): raw mics may be scored together, built beams may not** | score one stream only. C95, 37 attempts at 3 m: ch6 alone 6.75, one mic locked by SRP-PHAT at the wake 7.0, ch6 + 3 taps 16.0 (2026-11-22) |

## Speech gate and end of turn

| Method | Where | Source | Status | Fallback (cost) |
|---|---|---|---|---|
| Silero VAD | `client/speechgate.go`, `em_speechgate` | Silero VAD 2021 (MIT) | recent | energy VAD (Rabiner & Sambur 1975), already the device's fallback when the model is missing. Cost: residue opens the gate (4 phantom turns in 8 min over music, VVV 2026-09-22) |
| Hold audio until speech, release the last 1.2 s | `em_speechgate` | VAD hangover and pre-roll (GSM VAD, ETSI 1990s) | old | — |
| Two turn clocks (first real frame vs turn start) | `em_turnclock` | timeout logic | old | — |

## Noise suppression (controller, ASR stream only, off by default)

| Method | Where | Source | Status | Fallback (cost) |
|---|---|---|---|---|
| DTLN, dual-signal LSTM | `em_ns` (`nsAsr`, default off) | Westhausen & Meyer 2020 (MIT) | recent | Wiener filter with decision-directed SNR (Ephraim & Malah 1984); not measured on this path |

## Multi-Echo arbitration (controller)

| Method | Where | Source | Status | Fallback (cost) |
|---|---|---|---|---|
| First to hear wins, by capture time, within a window | `em_arbiter` | timestamp ordering (Lamport 1978); nearest sensor hears first (time of arrival) | old | — |

## Output chain (device and controller)

| Method | Where | Source | Status | Fallback (cost) |
|---|---|---|---|---|
| Biquad shelves and peaks | `outchain/eq.go`, `em_eq` | Bristow-Johnson, *Audio EQ Cookbook* (1990s) | old | — |
| Linkwitz-Riley 4th-order crossover | `outchain/bassguard.go`, `em_mbc` | Linkwitz 1976 | old | — |
| Dynamic bass removal / multiband compression | same, `stocksound.go` | multiband compression (broadcast, 1970s); thresholds read from the board's stock config (#229) | old | — |
| Look-ahead peak limiter with a sliding-max window | `outchain/limiter.go`, `em_limiter` | broadcast look-ahead limiting; monotonic-queue sliding max | old | — |
| EQ that follows the volume | `stocksound.go` | loudness compensation (Fletcher & Munson 1933; hi-fi loudness controls) | old | — |
| Ducking with a gain ramp | `bindings/speaker/mix.go` | broadcast voice-over ducking | old | — |
| Wake and volume cues: tones with raised-cosine edges | `cue` | trivial | old | — |
| Opus decode | `device/internal/opus` | RFC 6716 (2012), BSD | recent | — |

## Turn stage (in flight: `feat/turn-segment`)

Built only from methods with an old source, by decision (2026-11-21).

| Method | Source | Status |
|---|---|---|
| STFT / weighted overlap-add, sqrt-Hann | Crochiere 1980; Griffin & Lim 1984 | old |
| Steering by covariance subtraction, eigenvector by Jacobi | Gannot et al. 2001; Jacobi 1846 | old |
| MVDR with diagonal loading, fixed for the turn | Capon 1969; Cox et al. 1987 | old |
| Wiener post-filter, decision-directed SNR, gain floor | Ephraim & Malah 1984; Berouti et al. 1979 | old |
| Noise floor by minimum statistics | Martin 1994/2001 | old |
| One Silero + energy segment per turn | as Speech gate | recent / old |

## Shelved for want of a fallback

| Method | Why shelved |
|---|---|
| Wake or barge by scoring several separated or beamformed streams (online AuxIVA, fixed MVDR bank, the Dot 2 beam union) | Offline 37 TV attempts: raw mics 18, IVA 30-31, fixed beams 25-29. Shelved 2026-11-21 (`bench/iva-wake`); the line was drawn 2026-11-22: several raw mics may be scored, several built beams may not. One beam feeding one scorer is fine. |
| Voice signature for the turn | Parked 2026-11-21. |
