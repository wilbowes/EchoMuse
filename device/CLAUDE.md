# CLAUDE.md — `device/`

The Go binary that runs on the Echo Dot. Project-wide direction, the
device/controller compatibility rules, the wire protocol and the release
scheme are in the repo-root `CLAUDE.md`; the controller half is in
`controller/CLAUDE.md`.

## Building the device binary

The Echo Dot runs FireOS 5 (API 22). Standard Go cross-compilation won't work — a custom Docker build environment is required.

**One-time setup:**
```bash
# GoTinyAlsa is a git submodule at the repo root — the wilbowes/GoTinyAlsa
# fork, NOT upstream Binozo: it carries the GetAudioStream defer-in-loop
# leak fix (v2.9.2). Don't repoint it upstream until that fix is merged there.
git submodule update --init

# Build the compiler Docker image (from device/)
cd device
docker build -t echomuse-compiler compiler/
```

**The compiler base is pinned by DIGEST, and must stay that way.**
`compiler/Dockerfile` carries the Go toolchain (1.24.0) and NDK
(21.4.7075529) that compile the firmware, so it is the layer sitting
directly on top of FireOS 5 — a 2015 platform that cannot be upgraded. It
was `FROM ghcr.io/binozo/echogo:latest`, a third party's floating tag, and
`release.yml` rebuilds the image **from scratch on every tag push**: every
release was free to pick up a different compiler than the last, with no PR
and no CI signal. The first symptom would be a binary the hardware refuses
to run, which is the one failure here not recoverable from the dashboard.

Moving the pin needs **a real device in the loop**. The host tests and
`go vet` cannot speak to it — they run on amd64 with the host toolchain,
and this image is exercised only by `compile.sh` and `release.yml`, so a
green CI run on a pin change proves nothing about it.

**Compile:**
```bash
cd device
./compile.sh
# Output: build/server
```

`compile.sh` embeds the git version string via `-ldflags "-X .../client.Version=..."`. Dirty trees get a `YYYYMMDD-HHMM-dev` timestamp instead of the tag.

**Run Go tests (host):**
```bash
cd device
go test ./...
```

Tests only cover pure-Go logic — hardware-dependent code is not testable on the host.

**Run controller tests (host):**
```bash
cd controller
python -m pytest tests/        # needs: pytest numpy scipy pyyaml — not the full requirements.txt
```

Controller tests cover the pure-logic modules only (`em_eq`, `em_limiter`, `em_mbc`, `em_scenes`, `em_oww_models`, `em_oww_warmup`, `version`, `em_hostip`, `em_ingressauth`, and the decision modules — `em_linkauth`, `em_button`, `em_shadow`, `em_turnclock`, `em_runbarrier`, `em_announce`) — keep it that way unless you're prepared to pull openwakeword/aiohttp into the test environment. Both suites (plus `go vet`) run in CI on every push/PR (`.github/workflows/ci.yml`).

**Release:** pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds the binary in the compiler image and attaches it to a GitHub release. **Tag with `git tag -a --cleanup=verbatim`** — the annotation message becomes the release body (`body_path` from `git tag -l --format='%(contents)'`), which is what the dashboard shows next to an available update. Write it for the person deciding whether to push firmware to a device they depend on: what changed, what to expect, anything required of them. GitHub's generated commit list is still appended below it. A lightweight tag yields an empty body and falls back to that list, which is a worse experience, not a broken one.

**`--cleanup=verbatim` is not optional if the notes use Markdown headings.**
`git tag -a` defaults to `--cleanup=strip`, which treats a line beginning with
`#` as a comment and deletes **the whole line** — so `## Volume` does not lose
its markers, it disappears entirely. v2.12.0 shipped that way: the notes were
structurally correct in the file, five headings gone from the published body,
and the only visible sign was a wall of paragraphs. Fixing it afterwards means
`gh api -X PATCH repos/<owner>/<repo>/releases/<id> -F body=@notes.md`
(`gh release edit` has no `--notes-file`), and re-appending GitHub's generated
commit list by hand, since the PATCH replaces the whole body.

## Device audio pipeline

Playback has a second plane: music rides `0x04`/`0x05` into its own buffer and
is mixed against voice at the ALSA write, so a voice turn **ducks** music
rather than pausing it. The rules for that mix — the constant-slew ramp, the
per-sample interpolation, `music_flush` vs `speaker_flush` — are under
"Ducking" in `controller/CLAUDE.md`.

Each mic buffer passes through, in order:

```
raw 9ch S24_3LE → beamformer + fixed mic gain (micGainDb, applied to 24-bit samples) → mono S16_LE → [AEC] → [AGC] → [VAD gate] → /data WebSocket
```

Note the real buffer cadence: GoTinyAlsa's `GetAudioStream` reads the whole ALSA buffer per chunk (PeriodSize 512 × PeriodCount 5), so the mic pipeline runs on **160ms batches of 2560 samples**, not single 32ms periods. Anything assuming 512-sample buffers must handle multiples (this silently disabled AEC for four releases — see `aec.Process`).

The always-on wake stream (`mic_start` without `lock_mic`) is **ungated and AGC-free**: every 32ms period is sent continuously (batched into 80ms frames) so openwakeword scores an uninterrupted stream, and no adaptive gain state can drift with room noise. The VAD gate and AGC apply only to bounded `lock_mic` turn streams (button-triggered), which get a fresh `ResetAGC()` per stream.

- **Beamformer** (`internal/beamformer/`) — selects the perimeter mic with the highest onset energy ratio (fast/slow EWMA) at voice turn start, then locks for the duration. Its `extractChannel` also applies the fixed mic gain (`micGainDb`, default +24dB) against the full 24-bit sample before quantising to S16 — captured speech sits at ~−70dBFS, so gain must happen pre-truncation to recover real resolution. `vadThreshold` stays in pre-gain units (the device scales it by the gain internally). **It is a selector, not a summing beamformer, and that is settled — do not propose delay-and-sum.** A frequency-domain implementation (exact FFT phase shifts, no interpolation artefacts) exists in `device/tools/bf_capture` and was measured as only marginally better than mic selection. The reason is the 72mm aperture, not the code: diffuse-field noise coherence is 0.84–0.99 below 1.5kHz where speech energy lives, so a sum has almost nothing uncorrelated to cancel, and 36mm adjacent spacing puts spatial aliasing at 4.76kHz — a working window of roughly 2–4.7kHz. Superdirective/differential beamforming is the only class that works at this aperture and it trades against white-noise gain (20dB+ amplification of sensor self-noise) on unmatched capsules across four ADCs. Full derivation and the coherence table are in SETUP.md's mic-array section (SETUP.md is the architecture reference; the chronological log is JOURNAL.md, the rooting prerequisites docs/rooting.md). **Far-field reach is therefore not a beamforming problem here** — it is room noise floor, distance and placement; the single-channel levers (`nsAsr`, wake model) are the ones that exist
- **AEC** (`internal/aec/`) — speexdsp echo canceller (vendored C, SpeexDSP-1.2.1), whole mic path including the wake stream; far-end reference tapped at the speaker ALSA write (every period incl. silence), delayed by `aecDelayMs` — **keep 0**: the mic side's 160ms batch reads absorb the speaker's output latency, and higher values make the echo non-causal (zero cancellation). The mic ALSA ring is only 160ms deep, so >160ms capture stalls silently lose whole batches (~every 20–30s in steady state, load-correlated); an occupancy governor trims the resulting reference backlog **without resetting the filter** — the trim restores the alignment the filter converged against, and the reset that used to live there thrashed convergence to ≤5dB (the v2.7.8 fix). `[aec] att=`/`far:` telemetry logs ~1/s during playback; `[mic] clock/stall` lines track capture loss. `far:` carries `rms`, `mean` and `peak` — **rms alone cannot tell audio from a constant offset**, since both read high, and that ambiguity cost an evening on #117 where the device was writing rms≈4000 to a codec while every speaker stayed silent. `mean≈±rms` with a small peak-to-peak is a DC offset; `mean≈0` with peak well above rms is real audio and the fault is downstream. Note this tap sits after the (L+R)/2 downmix and 3:1 decimation, so DC survives intact but `peak` is mildly smoothed — read it as a floor. It reports only while `aecEnabled`, so a diagnosis that needs it must not have AEC turned off. Default off (`aecEnabled`); ~14dB per response, held across turns
- **Barge-in** (controller-side `_barge_watcher`) — wake word spoken during TTS cancels playback (device does a stateful `speaker_flush`: drains buffer + discards until stream EOS, since the rest of the stream is typically still in TCP buffers; controller-side, both `stream_speaker` and the post-playback drain sleep race `cancel_event`). `bargeInThreshold` is used as-is and sits *below* `owwThreshold` by design (0.05–0.10): echo at the mic is ~25dB louder than the person, so speech-over-TTS scores are depressed (~0.3–0.5 observed), while converged self-echo scores 0.002–0.003. **A barge must abort HA's run before starting the interrupting turn** — see the voice backend section in `controller/CLAUDE.md`
- **AGC** (`internal/processor/`) — lock_mic turns only; release is frozen during silence (RMS speech flag), preventing noise floor amplification. (Device-side RNNoise NS was removed 2026-07-12 — noise suppression is controller-side now: `em_ns.py`/DTLN on the ASR-bound stream, per-device `nsAsr` flag)
- **VAD** (lock_mic turns only) runs on pre-NS/AGC audio; opens gate after `VAD_SPEECH_MS` of speech, closes after `VAD_SILENCE_MS` of silence, then sends an end-of-speech sentinel

## Key Go packages

| Package | Role |
|---------|------|
| `cmd/server.go` | Entry point: wires hardware, callbacks, and clients together |
| `internal/client/control.go` | WebSocket client to controller `/control` — registration, message dispatch |
| `internal/client/data.go` | WebSocket client to controller `/data` — mic streaming, speaker playback |
| `internal/server/` | Local state machine: mute, volume, LED mode priority |
| `internal/config/config.go` | Global runtime config; env var defaults, overridden by controller push |
| `internal/bindings/` | Hardware drivers: mic PCM, speaker PCM, LED I2C, button evdev |
| `internal/wakeword/` | openWakeWord streaming feature pipeline (mel ring → 76-frame windows → embedding ring → classifier). Pure Go: inference sits behind the `Inferer` interface so the buffering is host-testable with no ONNX/cgo. Validated tensor-for-tensor against Python via a golden fixture (`testdata/`, regenerate with `gen_fixture.py`) |
| `internal/wakeword/ort/` | The `Inferer` implementation: ONNX Runtime via cgo. The library is **dlopen'd at runtime, never linked** (only the MIT C header is vendored) so a device without it boots normally and falls back to controller-side wake word — verified by the ARM binary needing only libdl/liblog/libc with zero undefined `Ort*` symbols. `DefaultOptions` (1 thread, XNNPACK, `allow_spinning=0`) is the measured optimum: 37.7% of one core against 243% for ORT's defaults. Don't "fix" the thread count — more threads lowers latency and *raises* CPU, the wrong trade for duty-cycled work |
| `internal/wakeword/shadow/` | On-device scoring that reports but never acts (see "On-device wake word"). `Push` must never block: inference runs on its own goroutine and drops frames when behind |
| `internal/wakeword/fixture/` | Shared golden-fixture parser, tolerance policy and `Verify`. Used by both the host test and `tools/oww_probe`, deliberately — the probe's answer is the trusted one because it runs on hardware, so it must be exactly as strict as the test by construction. Tolerances are relative to the **tensor's** scale, not per element: per-element relative error is meaningless for tensors straddling zero |
| `internal/bindings/als/` | Ambient light (ams **TSL2540** on i2c). Android does not expose it AT ALL — `dumpsys sensorservice` reports an empty list, nothing under `/sys/class/sensors`, no input device; it is visible only on the raw i2c bus, the same shape as the mute LED being on a different GPIO than the vendor HAL believed. Resolved **by name, not address** (`0-0039` is an enumeration accident). **The bus listing is not a hardware inventory**: both ALS names are registered by Amazon's board file, so a `tsl2540` at 0x39 and a `tsl2584tsv` at 0x29 appear on every unit whatever is soldered on (`modalias` is static kernel data). Which one answers differs by batch — ours have the 2540 and nothing at 0x29 (`taos_probe() err = -6`, ENXIO), the `G090LF096` batch has the 2584 instead, reachable only through IIO at `/sys/bus/iio/devices/iio:device0` (#90). A second-sourced part, not a driver fault, so the answer is to read the IIO sensor too, never to loosen the match to a `tsl` prefix. The **boot log is the real inventory** — both drivers probe on every unit and log what replied — but `dmesg` rolls, so it needs reading soon after a reboot. Never `unbind` the driver to experiment: it succeeds, leaves the `als_*` attributes in place, and the next read hangs the device until a power cycle. `Lux()` returns **nil, never 0** — a covered sensor reads a genuine 0. `Watch` reports a step change immediately (25% relative, 10-lux floor, measured noise ±1.5%); the steady value rides the ~30s stats tick. `Report()` says **why** there is no sensor (`ok`/`no_chip`/`no_attribute`/`unknown`, plus every i2c name it saw) and rides the register message as `ambient_light_status` — absence used to be logged only to the device's own stdout, which support bundles do not collect, so two users could not be told apart without a shell session (#90). The whole bus is enumerated **before** matching: returning at the match truncated the list on working devices, which is exactly the side you compare against |
| `internal/bindings/jack/` | Headphone jack detect (`/sys/class/switch/h2w`, mediatek accdet). Polled, not evented — the ACCDET input node reports no keys on this hardware. Exists for ONE job: accdet mutes `Ext_Speaker_Amp_Switch` on insert (correctly) and **nothing turns it back on**, so the speaker stayed dead until the next boot (#80). Output routing itself is done by the jack's own switch contacts — a response was heard in headphones while the mixer still read `Headphone_Speaker_Mux=Speaker`, so those controls do NOT describe where audio goes and nothing should drive them |
| `internal/wifi/` | Safe WiFi network change with auto-rollback (wifi_change/wifi_commit/wifi_scan control messages; pending-marker recovery at startup). Reload path is `svc wifi disable/enable` ONLY — see package comment for the hardware-proven constraints |
| `pkg/led/`, `pkg/mic/`, `pkg/speaker/`, `pkg/buttons/` | Hardware abstractions (interfaces) |

## On-device wake word (shadow mode)

The Echo can run the wake model itself. `owwOnDevice` = `off` (default),
`shadow` or `on`; an unknown value normalises to `off` at BOTH ends rather than
being guessed at — the two plausible guesses are "score silently" and "start
triggering", and one of those is a live behaviour change on a device that
cannot honour it. Neither end may assume the other is the careful one.

**`on` is gated on the `oww_trigger` capability, which is separate from
`oww_shadow` on purpose.** Shadow shipped first, so there is firmware in the
field that scores and reports without being able to act on it; offering those
`on` produces a device that scores perfectly and never answers.
`em_shadow.effective_mode` degrades `on` to `shadow` when the capability is
absent — never to `on`, which would leave the controller waiting for wakes the
firmware has no code to send while no longer acting on its own. That is a wrong
answer rather than the old behaviour, which is the line the whole capability
rule is drawn along.

### `on` — the device decides, the controller keeps watching

The device sends `oww_wake` (score, the threshold it actually cleared, and how
long AGO — never a timestamp) instead of `oww_shadow_cross`. It lands in
`Device.pending_wake` and the wake listener acts on it on its next mic frame
(~80ms), because that is where turn setup lives: capture routing, beam lock and
arbitration have to happen together, and driving them from the control-plane
handler would be a second copy of the most delicate sequence in the controller.
`em_shadow.decide_wake_source` is the decision, pure and tested, for the reason
`em_button.decide` and `em_linkauth.decide` are.

- **The controller keeps scoring, and its detections stop triggering.** Its
  score still records whether it agreed (`turns.ctrl_wake_score` /
  `ctrl_wake_delta_ms`, schema v17) — the comparison that justified shipping
  this, with the roles inverted, and the only place a *controller* miss can be
  seen at all. Without it, turning a device `on` would silently end the
  measurement: every turn would show a device score with nothing to compare
  against, which reads as perfect agreement rather than as no data.
- **It is also what leaves barge-in alone.** Barge is scored controller-side
  over the turn's own audio (`_barge_watcher`) and is untouched by this.
- **`last_wake_mono` is the CROSSING instant, not arrival.** Using arrival
  would fold the network hop into every comparison and every arbitration
  decision, which this fleet's measured 1.1–2.6s RTT excursions make certain
  to matter.
- **Mute is checked on the device** (`onWakeCrossing`), not only controller-
  side. The existing `mic_start` refusal plus the hardware ADC mute already
  make a muted wake harmless, but "harmless" still means the ring lights up and
  HA runs a pipeline because a muted device thought it heard something. The
  crossing is still *reported* — it is real data about the detector.
- **A pending wake expires** (`MAX_PENDING_WAKE_S`, 4s, measured from the
  crossing). A wake that stale means the person has finished speaking, so
  acting on it answers into silence; expiry logs the age, which is the
  instrument for whether the trigger needs more slack.
- The trigger label is `wakeword-dev(score)`, which still matches every
  existing reader's `wakeword` prefix — including `_persist_turn`'s shadow
  block.

**Arbitration is NOT yet corrected for this.** `_wake_arbiter.claim` still
compares arrival order, so on a multi-device fleet a device can lose a 700ms
window because its claim was late and the wrong room answers. The fix is to
compare RTT-corrected times, never revoke a granted claim (that cuts a turn
already speaking), and hold the window longer than it is measured. Single-device
use is unaffected — solo fleets skip the window entirely.

**Shadow mode scores and reports; it never acts.** It exists to answer whether
on-device detection is good enough to trust, by comparing both detectors on the
same audio. The tap sits where the ungated wake stream's frames are written to
the wire, so the device scores byte-identical 80ms frames on identical
boundaries — a score difference can then only be the engine, not the framing.

Three things are load-bearing:

- **Inference must never run on the mic goroutine.** It costs ~31ms per 80ms
  frame, the mic loop reads 160ms ALSA batches, and the ring is only 160ms
  deep — two frames inline would spend 62ms of that budget and risk the capture
  stalls that lose whole batches. `shadow.Scorer.Push` hands off to a buffered
  channel and returns; the scorer goroutine **drops frames and counts them**
  when it falls behind. A shadow run that drops frames is informative; one that
  stutters the microphone is not.
- **Nothing is sent per frame.** Threshold crossings go immediately (they are
  rare — a refractory period collapses each utterance to one — and their whole
  value is the timing). Everything else is a window summary riding the existing
  ~30s stats tick, so the DB cost is one extra upsert per 30s per device.

  The window summary carries `maxInferMs` and `maxGapMs` (schema v16) because
  `dev_drops` alone had stopped being able to answer its own question — turn
  bursts, core hotplug and controller redeploys were each falsified by
  measurement. A drop means the 8-frame (640ms) queue overflowed, which has two
  causes needing opposite fixes: the slowest single inference is the CONSUMER
  stalling, the longest gap between frames arriving is the PRODUCER bursting.
  **Maxima, not averages** — a stall IS the tail, and a 700ms event averaged
  over 375 normal frames disappears. They are `atomic.Int64`, not mutex state,
  because `Push` runs on the mic goroutine; and the gap is measured in
  `enqueue` so one site covers `Push` and `PushBytes` alike, the same reason
  drops are counted in exactly one place. Note the first frame must record NO
  gap: a zero-valued `lastPush` would report a gap of however long the process
  had been running and point at a producer stall that never happened.
- **The device never sends a timestamp.** An Echo's wall clock is bogus before
  NTP, so it reports how long *ago* a crossing happened and the controller
  converts against its own monotonic clock — same reasoning as the RTT
  instrumentation.

**Thresholds must match or the comparison is meaningless.** The controller drops
its wake bar to `bargeInThreshold` while the speaker is streaming (echo at the
mic is ~25dB louder than the person, so speech-over-TTS scores are depressed), so
the device mirrors that: `shadow.Scorer.SetBargeThreshold` uses the lower bar
while `PcmSpeaker.IsStreaming()` is true, and never *raises* the bar if
misconfigured above the normal one. The device reports the threshold in force
with each window summary; it lands on the turn as `dev_threshold` (schema v15).
`turns.wake_threshold` now records the **effective** threshold the wake actually
cleared, not the nominal one — recording 0.5 for a wake that fired at 0.055 made
rows self-contradictory (present in data since at least 2026-07-25) and made
every barge-in look like an on-device miss. The activity rollup therefore reports
three buckets, not two: agreed, missed, and **not_comparable** (controller used a
lower bar, or the device's threshold is unknown).

Correlation (`em_shadow.ShadowTracker`, schema v13) happens at turn-persist
time, not at detection: the crossing report can land after the wake it belongs
to, and by turn end it has had seconds to arrive. The nearest crossing within
`MATCH_WINDOW_S` (2.0s) wins and is **consumed**, so two turns in quick
succession cannot both be credited to one crossing. The window is loose on
purpose — both detectors see the same frames but not in the same detector
*state*, since the controller drops wake frames while a turn or TTS is in
flight, and a false "miss" argues against a feature that is actually working.
`turns.dev_shadow` records whether the device was scoring at all, which is what
separates "the device missed this" from "the device was not looking";
`wake_counters.dev_*` carries the hourly view, where crossings with no matching
turn are the false-accept side that per-turn rows structurally cannot show.

Requirements and cost: ONNX Runtime plus the three models must be installed at
`shadow.DefaultDir` (`/data/local/share/echomuse/oww`, override `EM_OWW_DIR`)
— they are **not** in the firmware, since 12.3MB would double the OTA payload
and both A/B slots. Absence is an ordinary condition, logged once, and the
device carries on with controller-side wake word. `device/tools/oww_probe`
verifies a device reproduces Python and reports the real CPU cost. It costs
~38% of one core permanently on top of the ~18-20% mic-pipeline baseline, so
**enable it on one device at a time**.

**The scorer pointer must be re-read PER FRAME, never cached for a stream.**
A config push replaces the scorer and **closes** the old one, so a mic stream
holding the pointer it captured at `StartMic` is feeding a dead object. This
cost two bugs in succession on 2026-08-16, and the second is the instructive
one:

- `Close()` used to close the channel `enqueue` sends on, so the next 80ms
  frame panicked the process. It restarted in ~6s, opened a fresh mic stream,
  picked up the new scorer, and worked — the crash was **accidentally
  self-healing**.
- Making `enqueue` drop silently removed the crash *and* the recovery. The new
  scorer then received nothing and detection stayed dead until the next
  `StartMic`, which only follows a voice turn, which cannot happen because the
  wake word is dead.

So `Close()` signals a dedicated `quit` channel and never closes `ch`, AND the
push site re-reads `d.ShadowScorer()` per  frame. A mutex read per 80ms is
nothing beside the inference it feeds. Both halves are needed; either alone
leaves a device that goes deaf or panics. The comment that justified caching
("a stream that began before the change keeps using the scorer it started
with") described exactly what made it fatal.

**A missing classifier used to silently deafen a device under
`owwOnDevice=on`.** The device cannot score without the model, and the
controller has stood down and no longer triggers on its behalf — so nothing
fired, nothing warned, and the dashboard reported the device as healthy. That
is a degradation to *no* behaviour, which the capability rule above exists to
forbid. Selecting a wake word a device was never provisioned with is enough to
produce it, and that is an ordinary dashboard action.

**Bouncing the HA connection flaps EVERY entity for that device.**
`update_oww_model` drops and remakes the connection so HA re-reads the wake
word, and that is the only lever the protocol offers — HA calls
`_update_satellite_config()` from `async_added_to_hass` and nowhere else. The
cost is that the voice assistant, media player, event and sensor entities all
go unavailable and back in the same instant.

For the **event** entity that is user-visible and looks like a fault: HA's
`EsphomeEvent._on_device_update` deliberately writes state on reconnect
("Event entities should go available directly when the device comes online"),
restoring the last event's timestamp. `_trigger_event` is NOT called, so HA
does not think a new event happened — but a **state-triggered** automation sees
`unavailable` → timestamp and fires. Reported 2026-08-17 as "changing the wake
word triggers a long button press"; the controller had sent no event at all.
The user-side fix is `not_from: [unavailable, unknown]`, documented in
docs/configuration.md. Worth remembering before adding any new bounce.

**The fix is install-before-switch, not a fallback.** A device is never told
about a new `owwModel` until the classifier is on it
(`em_api._hold_back_oww_model` swaps the key back to the current model, and
`_install_then_switch` pushes the real config once the file has landed). The
device keeps listening for its CURRENT wake word, on-device, throughout; if the
install fails it simply stays there and says so loudly.

The first attempt stood the device down to controller-side scoring while the
model installed. That works and was rejected: it silently overrides a setting
the user chose, and the dashboard goes on reporting `owwOnDevice: on` — the
same "reports healthy while something else is true" shape the capability rule
exists to forbid. Note there is no privacy difference between the two modes
(the device streams the wake audio either way, and the controller scores it in
`on` mode too — that is what `turns.ctrl_wake_score` records), but a silent
override is a trust problem regardless.

Only devices that actually score locally are held back — with
`owwOnDevice=off` the file is irrelevant, so the change stays instant. Both the
old and the incoming mode are consulted, or a save that enables on-device
scoring while changing the wake word slips through on the old mode.

`em_shadow.effective_mode` also takes `model_ready` alongside
`trigger_capable`, as a **backstop** with no current writer: install-before-
switch removed the case that set it, and its intended writer is the
reconcile-on-connect pass designed in #191 — the first thing that will actually
know what a device has. A known-missing model degrades to **`off`, not `shadow`** —
shadow cannot score either, so degrading to it would be the wrong answer
dressed as a fallback; only `off` puts the controller back in charge of
triggering, which is the one arrangement that still answers the user. It
defaults **True**: absence of evidence is not evidence of absence, and standing
every device down because the controller has not looked would be worse than the
bug.

The hold-back is invisible at the call site — the config push looks entirely
ordinary and the whole guard is that one key was swapped out first — so tests
pin the ordering, the capability gate, and that a failed install returns rather
than falling through into the switch. The install runs as a background task:
blocking the config save on a multi-megabyte shell-plane push would time out
the request without making anything safer, and nothing is degraded while it
runs.

**Every device carries all four stock classifiers**, not just the one selected
when it was provisioned (`em_oww_assets.STOCK_MODELS`, 3.04MB for the set).
That removes the whole class of "you selected a wake word this device has never
had" for stock models — which under `owwOnDevice=on` is a device with no wake
word at all. The set is what `dashboard.jsx`'s `WW_MODELS` offers, pinned by
test in both directions: a wake word offered but not installed is #191, and one
installed but not offered is dead weight. openwakeword's `timer` and `weather`
are NOT included — they are intent models, not wake words, and are offered
nowhere.

Both transports share `desired_assets` (`include_stock` defaults True), so a
device provisioned today and one synced today carry the same files.

**`CLASSIFIER_SLOTS` budgets LEFTOVER CUSTOM classifiers, not every classifier
on the device.** It used to be a budget for all of them, which was right while
only the selected model was ever desired; the four stock models fill it exactly,
so under the old rule installing them would have silently deleted every custom
model on the device — including ones a user trained and cannot re-download.
Stock models are required by definition and never evictable.

The rest of #191 — custom slots, reconcile-on-connect and a per-device Repair
action — is designed on the issue and not yet built. A **custom** model
selected on a device that never received it still reproduces the silent
failure.

### Asset distribution (`em_oww_assets.py`)

Installing those files is automatic. `em_oww_assets` plans (pure, unit-tested);
`em_api` carries it out. Two transports, one plan: the **provisioning wizard**
pushes over USB/ADB (a fresh device is not connected to the controller yet, and
USB suits 15MB far better than a base64 heredoc), and **fielded devices** use
the shell plane from the device's Updates tab. The wizard step is **mandatory**
— a device advertising `oww_shadow` without the assets is exactly the "I
enabled it and nothing happened" this removes.

- The ARM runtime is **vendored into the controller image**, pinned by AAR
  sha256 (`onnxruntime-android` 1.19.2), so devices never need internet. The
  models come from the installed openwakeword package or `oww_models/` — no
  second copy to keep in step.
- **md5 is the only definition of success**, both transports. Push to `.part`,
  rename only on match: a truncated file is the right size and fails later at
  `dlopen` with an error naming nothing.
- **Four classifier slots, LRU by device mtime** — no controller-side
  bookkeeping to lose across a restart. The selected model is **pinned**;
  evicting it is the one outcome that breaks a device rather than costing a
  re-push. Only files positively recognised as evictable classifiers are ever
  deleted.
- Free space is checked against **what actually needs sending**, so a device
  that already has everything is never blocked. Read it with
  `parse_free_mb`, never an awk field index — busybox wraps a long filesystem
  name onto its own line, so `$4` is the *percentage* on these devices, which
  parsed as "unknown" and silently disabled the check.
- **`TransferResult` is truthy-compatible so existing `if not …` call sites
  keep working — which is exactly how a call site that treated it as a LIST
  reached a release.** `_sync_oww_assets` assigned the per-file transfer
  result over its own `pushed` accumulator, so the first file replaced the
  list and the append raised `AttributeError`. Every classifier push 500'd,
  and provisioning is the only other path that installs one, so a device
  could never be given a wake word it had not been provisioned with. Pinned
  by test.
- `DEVICE_DIR`, the shared model names and the classifier stem rule are pinned
  against the firmware constants **by test**. Drift installs assets the device
  never looks for, and the only symptom is shadow mode silently never starting.

## The external audio jack

Three separate faults, all fixed 2026-08-09 (#80), and one non-fault worth
knowing so nobody builds it.

**Boot with a plug inserted used to strand the whole device.** Android's
mediaserver claims the speaker PCM when a headset is present, and ALSA parks a
blocking open behind it with no timeout:

```
/proc/<pid>/task/<tid>/wchan          -> snd_pcm_open   (parked indefinitely)
/proc/asound/card0/pcm23p/sub0/status -> PREPARED, owner_pid: 659
fuser /dev/snd/pcmC0D23p              -> 258 (/system/bin/mediaserver)
```

`main()` initialises the speaker **before** `SubscribeToButton`, mDNS and the
control client, so one held device cost everything — no buttons, no wake word,
no registration. That is the whole of the "no wake word and no working
buttons" report. `Init()` now runs `stop media` first (the same stock-service
takeover as `stop mixer` beside it and `stop smarthomewifid` in `main`) and
waits on the substream status before opening.

**`stop media` does NOT stick, and the fix does not depend on it doing so.**
Android restarts mediaserver — measured on hardware: `init.svc.media` reads
`running` again, with a live pid, while our server still owns `pcm23p` in
`RUNNING` state. What makes this work is winning the race ONCE and then
holding the device for the life of the process, and `Init()` re-runs
`stop media` on every start, so an OTA or a supervisor restart gets the same
treatment. Do not "improve" this into a permanent disable: mediaserver
returning is what keeps Amazon's audio HAL — and therefore the DSP and the
I2S clock — initialised, which SETUP.md's Audio Notes describe as load-bearing.

**Android still reacts to jack events.** With mediaserver back, an insert
makes its `AudioOut_2` thread reconfigure the amp, DAC mux and ramp
underneath us (`EXTAMP Enable=0`, `Audio_DacMux_Set()`, `set_ignore_ramp`).
Removal produced no such reaction — only our own `tinymix`. Worth knowing
before blaming our code for codec state changing without us. **The speaker is card 0 device
23**; the mic is 24. Checking `pcm0p` reads `closed` and proves nothing — that
is Android's own device, and mistaking it for ours cost a wrong conclusion.
On timeout it opens anyway, which is the pre-existing behaviour: the wait is
there to make the common case work and to leave a log line naming the holder.

**The log looks healthy while this happens**, which is most of why it went
undiagnosed: `[mic] clock` lines keep appearing every 60s from a goroutine
started before the block. The tell is what is *missing* — `PcmSpeaker
initialised` never appears.

**Unplugging left the speaker silent until the next reboot.** accdet mutes
`Ext_Speaker_Amp_Switch` on insert, which is correct — the Dot should not also
play to the room — and nothing ever turned it back on. `Init()` was the only
thing that set it, which is exactly why a reboot appeared to fix it.
`internal/bindings/jack` watches `h2w` and re-enables the amp on removal.
Insert needs nothing from us.

**Routing is PHYSICAL and needs no code.** The jack's own switch contacts
divert the signal. A voice response was heard in headphones while the mixer
still read `Ext_Speaker_Amp_Switch=On`, `Ext_Headphone_Amp_Switch=Off` and
`Headphone_Speaker_Mux=Speaker` — so those controls do **not** describe where
audio goes, and driving them would be wrong. Do not build a routing layer.

**Unresolved, and it is not only on removal (#117, #141).** A plug in the
jack degrades the whole audio subsystem for as long as it is present — mic
capture stalls on a ~102.3s metronome, and output dies and cycles between
three audible outcomes, recovering instantly on removal. Downstream the
controller sees `no mic frames for 10s`, breaches `ping_timeout` and tears
down the ESPHome satellite, BLE proxy and data plane, and **that teardown is
what users report** as music pausing and skipping (#141).

**Characterisation, exonerated surfaces and the live hypothesis are on issue
#117 and in JOURNAL.md (2026-08-12) — read them before touching this.** The
short version, so nobody repeats the work: all 6016 codec registers, all 218
MediaTek SoC audio registers and the full ALSA mixer are IDENTICAL between
audible and silent, so **do not go looking there again**; the fault is
load-independent (3-pole, 4-pole and headphones all fail); a stock Alexa Dot
plays the same speaker correctly, so it is ours, not the hardware; and the
live hypothesis is the audio HAL, which we displace by taking `pcm23p`.

Two traps for whoever picks this up:

- **`Ext_Speaker_Amp_Switch` does not gate the internal speaker.** It was
  `Off` while the internal speaker was audibly playing. accdet also mutes
  it on insert only *sometimes*. Making that deterministic ourselves looks
  like an obvious fix and is currently the wrong one: accdet failing to
  mute is the only reason a user hears anything at all with a plug in, so
  it would turn "wrong speaker" into "no sound".
- The mic stall log line says "ALSA overrun", which is an interpretation.
  It measures the arrival gap in `readLoop`, and the GoTinyAlsa stream
  channel is 16 batches (2.56s) deep, so a stall of that goroutine looks
  the same. The growing clock deficit does show audio is genuinely lost.


**Stereo is not supported and the device end is not the blocker.** ALSA is
already opened with two channels and `PumpPeriod` duplicates L=R; the mono
downmix happens at the controller, in three ffmpeg calls (`-ac 1`). That was
right when a mono internal speaker was the only output. With a jack it throws
information away — see the stereo issue rather than reinventing the analysis.

**`tinymix` IS on these devices** (`/system/bin/tinymix`), and the codec
regmap is readable at `/sys/kernel/debug/regmap/2-0018/registers`
(`tlv320aic32x4`). Do not drive `tinyplay` while the server is running: it
contends for the same PCM and wedged a device hard enough to need a power
cycle.

## CPU topology, thermals and why `cpuPct` lies

The MT8163 is a **quad-core** Cortex-A53 (`/sys/devices/system/cpu/present` =
`0-3`) and MediaTek's hotplug strategy parks all but cpu0 when idle. So
`/proc/cpuinfo` showing one processor is a **power state, not a limit** — a
mistake worth not making twice, because it turns a comfortable measurement into
an apparent ceiling.

HPS (`/proc/hps/`) governs it: `up_threshold=80` / `up_times=2` bring another
core online after two samples above 80% utilisation, `down_threshold=70` /
`down_times=20` park it again (slowly), `rush_boost_threshold=98`,
`input_boost_cpu_num=2` boosts on button presses. cpu0 runs at 1.3GHz — its
maximum — under the `interactive` governor, so no frequency headroom is being
withheld. The `num_limit_*` files are ceilings (all 4 = nothing capping);
**`num_base_perf_serv` is the FLOOR**, and the firmware raises it to 2 at
startup (`applyCoreFloor`). That is deliberate: the mic pipeline has a hard
160ms deadline and now shares a core with wake word inference running in ~31ms
bursts, and a floor of 2 lets them run in parallel instead of relying on
hotplug reacting to a burst that has already begun. It is procfs, so it does
not survive a reboot — hence applying it in the binary, which re-applies every
start. Do NOT write `cpu1/online` directly: HPS re-parks it within
`down_times`, giving a setting that appears to work and silently stops.

**`cpuPct` is a share of ONLINE capacity**, derived from the aggregate
`/proc/stat` line. The same absolute work therefore reads as *half* the
percentage once a second core comes up — measured on Lounge, 51% on one core
became 25.5% on two with the workload unchanged. Always read it next to
`coresOnline`; a `cpu_avg` series without the core count can show a "drop" that
is purely a change of divisor. That is why both are reported and persisted.

Thermals: 11 zones. `mtktscpu` is the CPU/SoC (reported as `cpuTempC`),
`mtktspmic` the PMIC and `tmp103` a discrete board sensor; `maxTempC` is the
hottest of all of them, because trouble does not always appear on the zone you
thought to watch. Idle sits at 31–34°C, nowhere near throttling.
**`thermalCoreLimit` (`num_limit_thermal`) is the sharpest throttling signal
this SoC offers** — below `coresTotal` means the governor is already capping
capacity, which bites well before any temperature reading looks alarming.

## Volume / mute persistence

**The scale stops at the codec's unity gain, and that ceiling is load-bearing.**
tinymix ctl 61 is the tlv320aic32x4 DAC *digital* volume: 176 steps of 0.5dB
spanning −63.5…+24dB, with 0dB at index **127**. The firmware shipped
`volumeMax = 175` — the control's own maximum — so the top 27% of the range
applied up to +24dB of digital gain to already near-full-scale PCM and
saturated inside the DAC. Measured on hardware 2026-08-13 (1kHz at −6dBFS,
recorded through the mic array): THD 1.5% at index 127, 2.3% at 136, **65% at
153, 89% at 170**, with the output level *flat* from 153 upward because it had
stopped being able to get louder, and h3 at −1.1dB relative to the fundamental
(very nearly a square wave). The control that isolates it: index 170 with the
source scaled down to land at the same acoustic level reads 1.1% — clean — so
the gain stage is fine and it is purely source × gain exceeding full scale.
Stock FireOS never writes this control **at all** (absent from
`/system/etc/audio_device.xml` and from every `/system` binary), leaving the
DAC at its 0dB reset default and taking user volume from AudioFlinger's
software attenuation, which only ever attenuates — that is why native Alexa
has no such distortion.

Two things not to undo: `DEVICE_VOLUME_MAX`/`volumeMax` stay at 127 (both
pinned by test), and the conversion lives in **one** place — `em_volume.py`,
because `level / 175` was copy-pasted into `em_controller`, `em_esphome` and
`em_api` with no test on any of them, which is how the wrong ceiling survived.
The lost headroom **cannot** be bought back from `Ext_Amp_Gain` (ctl 13): that
control is inert on this board — sweeping its full 6/12/18/24dB range moves
the output 0.0dB while still reading its new value back, the same shape as the
mute LED being on a different GPIO than Amazon's own HAL believed.
`HP Driver Gain Volume` (ctl 62) *is* live (+18dB commanded → +18.1dB actual,
THD 2.25%) if more output is ever wanted, but that is a taste call to make by
ear, and the speaker's behaviour above stock level is unmeasured.

The **physical buttons** traverse `volumeButtonFloor`(47, −40dB)…127 in 4dB
steps rather than the whole control: the scale is dB-linear, so the bottom
third is indistinguishable from silence and stepping across it spends presses
to go nowhere. Silencing the device is the mute button's job. Explicit `Set()`
calls are deliberately **not** floored — HA's volume 0.0 must still mean
silent — and a press from below the floor lands *on* it, so one press always
reaches audible.

Volume is **state, not a setting** — it rides the config channel but has no dashboard control (the slider was removed 2026-07-25: `SeedVolume` ignores later pushes, so moving it did nothing until the device restarted and any real volume change overwrote it). It is listed in `em_config_sections.STATE_KEYS`, exempt from section scoping, and shown read-only on the Status tab.

Volume persists through reboots **controller-side**: every device `volume_state` report is stored into the device's `startupVolume` config, and the device restores it via `Server.SeedVolume` on the **first config push per run only** (later pushes must not stomp live changes). Until seeded (or a local volume change makes the device authoritative), the device suppresses its connect-time `volume_state` report — reporting the boot-default level is what used to clobber the stored value on reboot. Mute is the opposite: **device-sovereign**, persisted locally in `/data/local/etc/echomuse/state.json` (survives OTA slot flips; written on toggle, restored at boot pre-connect — ADC mute immediately, red ring/button LED after LED init).

## LED priority system

Turn-state ring colours (listening ring, thinking spinner) come from **LED scenes** (`em_scenes.py`), configurable per device (`ledScene` + custom colours). Firmware with the `led_anim` capability (v2.9+) **animates locally**: the controller sends one `led_anim` message per state change ({pattern: solid|spin|rotate|pulse|meter|off, colors, periodMs, ttlSec}) and the device renders frames on its own ticker (`internal/server/animator.go`) — controller/WiFi jitter can't judder the ring. `meter` throbs with the live speaker RMS (tapped at the ALSA write, so it tracks audible audio, not the ~5.5s-ahead send) — measured on the **voice plane only, before the music mix**, unlike the AEC far-end tap which deliberately sees the mixed output; a meter fed the mix throbs to the music bed before the response has started; its response curve is config-tunable (`meter*` keys → `AnimSpec` pointer fields → `resolveMeter`, which clamps independently of the dashboard ranges) because it is a taste parameter that needs iterating in a real room, not a firmware OTA per pass. `ttlSec` is bounded per phase — 30s listening, 135s spinner (**coupled to `_fetch_tts_audio`'s 60s timeout ×2 attempts, since the spinner spans HA think time AND the fetch — move one and move the other**), and computed per response for `meter` via `em_scenes.meter_ttl` so a long TTS cannot self-clear mid-answer. Loss-resilience: newer spec or raw `leds` frame atomically replaces the animation (generation counter), and `ttlSec` is a dead-man that self-clears the ring if the controller dies mid-turn. Legacy firmware falls back to controller-streamed frames. Controller `leds` messages carry an explicit `listening: true` flag on listening-ring frames — the device's direction overlay keys off it (pre-scene firmware inferred "listening" from an all-green ring, which breaks for any other scene; the heuristic remains as fallback for old controllers). The direction overlay brightens the base ring colour instead of painting green. Mute ring (red) and volume arc (cyan) are device-local and scene-independent by design.

Turn *outcomes* are distinguished by rhythm, not colour (red/orange/cyan are taken by mute/link/volume): `no_speech` gets one slow throb, `no_tts`/`tts_error`/`timeout` fast blinks, everything else ends silently. Both ride the existing `pulse` pattern with a 1s TTL so they retire on the device's own ticker — no follow-up message to lose. Driven by `device.last_turn_outcome` (set in `em_esphome._persist_turn`, consumed once by `_leds_turn_end`).

Playback ring clearing waits for the device's `playback_stats` (`device.playback_done`), NOT a wall-clock estimate. The old estimate subtracted socket-write time — which completes near-instantly however slow the wire is — so it cleared the ring up to 6.1s early on exactly the links that needed longest. `playback_stats` is emitted once the audio channel drains after EOS, i.e. the real end of audio; the timeout is only a backstop for the report never arriving.

`server.go` maintains a `ledMode` (direction arc vs. system). System-level LEDs (controller commands, mute ring, pulse animations) always win over the beamformer direction arc. Two paint suppressions in `SetLEDs`/`SetDirectionLEDs` (state is still recorded in `baseLEDs` so the ring can be restored):

- **Mute ring** (solid red) is device-sovereign — enforced since v2.7.8: controller LED writes are recorded but not painted while muted. Needed because muting now terminates an active turn (controller cancels + `speaker_flush` on `mute_state`), so the cancelled turn's LED cleanup arrives after the red ring is up.
- **Volume arc** owns the ring for its 2s display window against *animations* — they repaint ~every 100ms and would otherwise stomp the arc within one frame. It does **not** outrank a deliberate action-button press: a dot release calls `CancelVolumeDisplay()`, which drops the hold so the listening frame paints (it deliberately does not repaint — the controller's frame lands within an RTT, and clearing to black would put a dark gap between the two). The arc is protection from repaint churn, not from the user. On expiry the ring repaints the latest `baseLEDs` frame (`onDisplayExpire` → `paintBaseLEDs`), handing back mid-animation. The arc shows only for physical volume button presses (v2.9.5): remote sets and the boot-time volume seed apply silently (`volumeController.Set` showRing flag). The mute-button LED is sysfs gpio444, active-high — not the gpio445 in Amazon's `libled_hal.so`, whose constant is off by one and whose pad is muxed away (stock drives the pin via the `/dev/mtgpio` ioctl; see `mute_button.go`).

## cgo dependency

SpeexDSP C source (AEC) is vendored in `device/internal/aec/`. The compiler Docker image provides the ARM cross-toolchain. If adding new cgo dependencies, they must compile cleanly with the `echomuse-compiler` image against the FireOS 5 sysroot.
