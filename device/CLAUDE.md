# CLAUDE.md — `device/`

The Go binary that runs on the Echo Dot. Project-wide direction, the
device/controller compatibility rules, the wire protocol and the release
scheme are in the repo-root `CLAUDE.md`; the controller half is in
`controller/CLAUDE.md`.

The why behind these rules — incidents, measurements, worked examples — is in
`docs/history/device.md` under the same headings. Read the matching section
there before changing that area.

## Building the device binary

FireOS 5 (API 22) needs the custom compiler image; plain Go cross-compilation won't work.

```bash
git submodule update --init                       # GoTinyAlsa: the wilbowes fork, NOT upstream Binozo
cd device && docker build -t echomuse-compiler compiler/
./compile.sh                                      # → build/server
go test ./...                                     # host, pure-Go logic only
```

- **GoTinyAlsa stays on the fork** until upstream has its playback fix
  (WriteFrames blocks signals around `pcm_write`, #707) — else a
  signal-interrupted write silently drops the rest of the buffer (clicks).
- **The compiler base (`compiler/Dockerfile`, Go 1.24.0, NDK 21.4.7075529) is
  pinned by DIGEST and must stay so**: `release.yml` rebuilds it on every tag,
  and a drifted compiler shows first as a binary the hardware refuses to run,
  unrecoverable from the dashboard. **Moving the pin needs a real device in the
  loop** — CI never exercises this image.
- `compile.sh` embeds the version via `-ldflags "-X .../client.Version=..."`;
  dirty trees get `YYYYMMDD-HHMM-dev`. Controller tests: root `CLAUDE.md`.
- **Release**: a `v*` tag runs `release.yml`. **Tag with `git tag -a
  --cleanup=verbatim`** — the annotation is the release body the dashboard shows
  beside an update, and the default cleanup deletes every `#` line. Write it for
  someone deciding whether to push firmware to a device they depend on.

## Device audio pipeline

Music rides `0x04`/`0x05` into its own buffer and is mixed against voice at the
ALSA write (ducking: `controller/CLAUDE.md` → "Ducking"). Each mic buffer:

```
raw frame (the board's MicArray: biscuit 9ch S24_3LE, Dot 3 4ch S32_LE) → beamformer (a mic selector) + fixed mic gain (micGainDb, applied to 24-bit samples) → mono S16_LE → [AEC] → [AGC] → [VAD gate] → /data WebSocket
```

- **Batches are 160ms (2560 samples)**, not 32ms periods: `GetAudioStream`
  reads the whole ALSA buffer (512 × 5). Anything assuming 512-sample buffers
  must handle multiples (see `aec.Process`).
- **The always-on wake stream (`mic_start` without `lock_mic`) is ungated and
  AGC-free**, sent continuously in 80ms frames. VAD gate and AGC apply only to
  `lock_mic` turns, with a fresh `ResetAGC()` per stream.
- **Beamformer** (`internal/beamformer/`): picks the perimeter mic with the
  highest onset energy ratio at turn start and locks. `extractChannel` applies
  `micGainDb` (+24dB) BEFORE truncation to S16 (speech is ~−70dBFS);
  `vadThreshold` stays in pre-gain units. **It is a selector — do not propose
  delay-and-sum** (72mm aperture; derivation in SETUP.md). Superdirective is
  reopened (Wil, 2026-10-14), judged on wake word and commands at normal voice
  in household noise; only noisy-room captures can rank beams, and until they
  do the selector stays.
- **AEC** (`internal/aec/`, vendored SpeexDSP-1.2.1), whole mic path incl. the
  wake stream; default off (`aecEnabled`).
  - **Hardware reference (#385)**: ch8 is an in-frame loopback of our playback
    (+33 samples, inverted, pre-DAC); no ring, `aecDelayMs` or governor.
    Promoted only when BIT-EXACT ZERO while silent AND carrying audio while
    playing (`noteEchoRef`) — never on energy alone, which would cancel against
    a real mic. One-way. `EM_AEC_HW_REF=off|on` overrides (env var: a board
    property).
  - **One filter per microphone (#814)**: `aec.ProcessMicsWithRef` runs all
    seven every period, idle ones resting after 256ms of silent reference
    (`quietHold`); one saved file, `aec_echo_path.v2.bin`, all or nothing.
    Hardware reference only. `ProcessWithRef` is for `tools/aec_replay` and
    tests only.
  - **De-aliased (#975) — never "simplify" back to raw ch8**, which aliases
    content above 8kHz and passes every test that plays nothing up there.
    `dealias.go` matches ch8 into our 48kHz output and low-passes that; no match
    → raw ch8. Search runs on a copy outside the speaker lock, backs off after
    three misses, allocates nothing once locked. Armed, mics extract 6dB under
    `micGainDb` and get it back after (`aecHeadroom`).
  - **No volume scalar on the reference** (volume is in software before both
    taps; a scalar applies it twice). **Unity gain on reference extraction**,
    pinned by test (gain clips it and teaches a distorted path). Never raise the
    DAC above unity (127): the loopback stops matching a clipping DAC.
  - **Software tap** (no loopback): `aecDelayMs` **keep 0** (higher is
    non-causal). Capture stalls lose whole batches; the occupancy governor trims
    the backlog **without resetting the filter**.
  - `[aec] far:` logs only while `aecEnabled`; read `mean` beside `rms`
    (`mean≈±rms` is a DC offset, not audio).
- **Barge-in** (controller `_barge_watcher`): device `speaker_flush` drains and
  discards until EOS. `bargeInThreshold` (0.05–0.10) sits below `owwThreshold`
  by design (echo depresses speech-over-TTS scores). **A barge must abort HA's
  run before starting the interrupting turn** (`controller/CLAUDE.md`).
- **AGC** (`internal/processor/`): lock_mic only, release frozen in silence.
  Noise suppression is controller-side (`em_ns.py`, `nsAsr`).
- **VAD** (lock_mic only) on pre-NS/AGC audio: opens after `VAD_SPEECH_MS`,
  closes after `VAD_SILENCE_MS`, then an end-of-speech sentinel.

### The speaker's voicing (stock sound, #229, 2026-10)

- **Firmware announcing `stock_sound` always voices its speaker; no key turns
  it off.** The voicing is board data (`board.Speaker.Voicing`,
  `pkg/board/voicing.go`) measured on that board's driver; a board without one
  plays the plain chain and does not announce `stock_sound`.
- Order, on device with no plug: volume → user EQ → correction → bass guard +
  three upper compressor bands → limiter (`speaker.refreshStock`). **The law is
  on only with a voicing** (`stockLawOn`; a nil voicing panics). With a plug in
  or a controller processing the audio, the plain chain and law stay.
- Law `StockVolumeDb` (0.5dB/level + `CeilingDb`, applied in float before the
  chain); the button tone follows it (`CueVolumeGain`, capped at unity).
  Correction keeps its 80Hz high-pass and 65Hz notch; `VoiceFilter` is replies
  only. Upper bands need a smoothed attack and a hold (instant = grit).
- **The bass guard is held on** whatever `bassGuardEnabled` says, and the
  dashboard hides it for `stock_sound`. EQ presets are relative to the voicing;
  changing a preset's values strands its users at "Custom".
- Python (`em_eq`, `em_mbc`, `em_volume`) is the reference; Go is bit-exact to
  `outchain/testdata` — regenerate with `gen_vectors.py` after changing either.
  Stock's files and captures stay off the repo (Amazon's).

## Key Go packages

| Package | Role |
|---------|------|
| `cmd/server.go`; `internal/client/` | Entry point; `/control` and `/data` clients |
| `internal/server/`; `internal/config/` | Mute/volume/LED state; runtime config (env defaults, controller push wins) |
| `internal/bindings/` | Hardware drivers; each opens what `pkg/board` resolved, never a number |
| `pkg/board/` | Board identity (idme `device_type_id`), parts BY NAME, `Resolve`, emOS thermal tuning. `guard_test.go` fails on hardware numbers anywhere else |
| `internal/wakeword/` | Pure-Go openWakeWord feature pipeline behind `Inferer`; golden fixture `testdata/` (`gen_fixture.py`) |
| `internal/wakeword/ort/` | ONNX Runtime, **dlopen'd, never linked**, so a device without it boots. `DefaultOptions` (1 thread, XNNPACK, no spinning) is measured — more threads raise CPU |
| `internal/wakeword/shadow/` | On-device scoring; `Push` never blocks |
| `internal/wakeword/fixture/` | One tolerance policy for the host test and `tools/oww_probe`; tolerances relative to the tensor's scale |
| `internal/listen/` | Private-listening gate and `Resolve`; pure, clock-injected |
| `internal/bindings/als/` | Ambient light by name, TSL2540 (i2c `als_lux`) or TSL2584 (IIO `illuminance0_input`) per batch, tried in order (`board.LightSensors`), never a loose `tsl` match. **Never read `calibrated_lux`** (kernel corruption; test-guarded); `illuminance0_input` only with an idme `alscal` `ams_0_0` baseline, else `unsafe`. Never `unbind` it (hangs). **Poll 5s, not 1s** (dark reads flood ram_console, the only crash log). `Lux()` is nil, never 0. `Report()` → `ambient_light_status`, whole bus enumerated before matching |
| `internal/bindings/jack/` | `/sys/class/switch/h2w`, polled; `Watch` dispatches its STARTING state too (a boot has no edge) |
| `internal/outchain/` | EQ → bass guard → limiter on the mix at the ALSA write, bit-exact to `testdata/gen_vectors.py`. Inactive until the ack carries `output_chain` (never runs twice). No `%` per sample: 32-bit ARM Go has no divide |
| `internal/sendspin/` | Sendspin player (#89): pull scheduler (`SetMusicSource`), consulted only while `0x04` is empty. **Speaks aiosendspin 9.1.1's wire** (§6.4 of `docs/audio-states.md`); re-run `tools/sendspin_interop/run.sh` before trusting an MA upgrade. `internal/firewall` opens 8928 before `Start` and closes it on every stop (emOS drops inbound). `OutputClock` gates readings over 3ms off prediction |
| `internal/wifi/` | WiFi change with rollback; reload is `svc wifi disable/enable` ONLY. **SSIDs are 0–32 BYTES** (`ssid.go`): `ssid_hex`, byte compare, quoted or hex. Never in a shell command |
| `internal/bluetooth/` | HCI passive scan over `/dev/stpbt` (Bluedroid `pm disable`d first); `emit.go` filters |
| `pkg/led/`, `pkg/mic/`, `pkg/speaker/`, `pkg/buttons/` | Hardware interfaces |

## Boards: hardware is found by name (#541, 2026-10-04)

`docs/boards.md` is the porter's guide; these must stay true. Parts are named
in `pkg/board/hardware.go`, found once by `board.Resolve`, opened via
`board.CurrentLayout()`. Registered: biscuit, radar (Echo gen 2 — never "Dot
3"), donut (Echo Dot 3rd gen).

- **Only biscuit has a numeric FALLBACK; a new board never gets one.** It is
  logged and sent once per start as a `[board]` warning (`Layout.Problems`).
- **An unidentified device gets biscuit's layout incl. gpio444**, so ADD a
  board before running firmware on it (on the Dot 3 that pin is an audio clock).
- **Ambiguous is refused, never first-match. Inputs match by NAME, never by key
  capability. The Bluetooth node is stated, never probed.**
- **Board differences are data in `Hardware`, never a branch in a driver**
  (`TestNothingBranchesOnBoardIdentity`). Fixture:
  `pkg/board/testdata/biscuit-fireos5/`.
- **`server board`** prints the layout and changes nothing — run it from a temp
  path beside the live server before installing a build that changes this.
- **Radar** = biscuit + its own speaker data: `AmpMute` (`MFP Gpio Mute`,
  asserted by the driver at every open/close); `Filter`, read at start-up from
  the unit's own `audio_device.xml`, never compiled in (zeros = silence;
  values are Amazon's); `Setup` (`DRC Control` Disabled); `BassGuard`
  70Hz/−25dB.
- **Start-up is amp, DAC, then mute released once RUNNING** (~1.25s ramp);
  `Ignore Ramp Up` clicks.
- **Capture is a `MicArray`**: channels, format, edge mics in steering order
  with angle, centre or −1, loopback or −1. Mics are channels 0..NumMics−1
  (test-held); `beamformer.New(array)`, `aec.New(nMics)`; no centre → wake on
  the first edge mic.
- **Donut** (4ch S32_LE, no centre mic): **`Loopback` (`DL1_AWB_Record`) must be
  open AND delivering before the mics open**, or the TAS2770 loses its clock
  until reboot; the binding waits for audio, retries 4s, and a stated loopback
  the kernel lacks means NO mics. `MicMutes` = ADC_A, ADC_B. `RingLED0` 240
  biscuit / 195 donut (`ringLEDFor`). idme `miccal` not applied.
- Still biscuit's inside drivers: amp switch, DAC unity, jack tables, `stop
  <service>` names.

## Private listening (the default since 2026-09-21)

Spec: `docs/listening.md`. Under `owwOnDevice=on`, with a controller announcing
`listen_session` and a scorer loaded, the device is `local`: **nothing is
sent** until a crossing opens a `listen.Gate` session, which sends `0x07` frames
from `listen.WakeLead` (260ms) before the crossing until `listen_close` or a
device limit. `listen.Resolve` decides the state; `syncListenState` reports
`listen_state` on every config push and connect.

- **The limits are the device's**: no `listen_ack` in 3s, 30s max, mute,
  `StopMic`, data-link drop. No controller failure may leave an Echo streaming.
- **Missing scorer is `degraded`, never `stream`.** The button still works.
- **One timestamp per frame**, given to both `PushBytesAt` and `Gate.Push`;
  crossings carry CAPTURE time.
- **`WakeLead` is counted in FRAMES back from the crossing frame**, not time;
  the pre-lead part of the first frame is silenced on a copy (shared buffer).
- A crossing inside an open session is words — `Gate.Open` refuses.
- **`mic_stop` ends only a lock_mic turn**, never local listening or a session
  (sessions end by id via `listen_close`). `mic_start` with lock_mic REPLACES
  the wake stream.
- **The barge bar applies to music too** (`speakerPlaying`), needs two
  consecutive frames, and is **dated from the FIRST**.
- **The Echo cuts its own reply at a barge crossing** (`listen.CutReplyOnBarge`)
  only while `replyStillHeard`. Do not drop reply audio from the session.
- Without `listen_session` the state is `stream` in every mode.

## On-device wake word (shadow mode)

- `owwOnDevice` = `off`/`shadow`/`on`; **an unknown value is `off` at BOTH
  ends**, never guessed.
- **`on` needs `oww_trigger`, separate from `oww_shadow`**; without it
  `em_shadow.effective_mode` degrades `on` to `shadow`, never to `on`.

### `on` — the device decides, the controller keeps watching

- Device sends `oww_wake` (score, threshold cleared, age — never a timestamp)
  → `Device.pending_wake`, acted on at the next mic frame where turn setup
  lives. `em_shadow.decide_wake_source` is the pure decision.
- **The controller keeps scoring without triggering** and records agreement
  (`turns.ctrl_wake_score`/`ctrl_wake_delta_ms`). Barge-in stays controller-side.
- **`last_wake_mono` is the CROSSING instant, never arrival.**
- **Mute is checked on the device** (`onWakeCrossing`); the crossing is still
  reported. A pending wake expires after `MAX_PENDING_WAKE_S` (4s).
- Label `wakeword-dev(score)` keeps the `wakeword` prefix readers match.
- **Arbitration compares capture times**: `capturedMono` and ping `mono` mapped
  via `em_listen.DeviceClock`, never arrival − age − RTT/2; controller-scored
  wakes via `CaptureClock`. A claim within the winner's window cedes; the
  winner holds window + 3s; a grant is never revoked.

**Shadow mode scores and reports, never acts**, on the byte-identical 80ms
frames sent to the wire.

- **Inference never on the mic goroutine**; `Push` hands off and the scorer
  **drops and counts** frames when behind.
- **Nothing per frame**: crossings immediately, the rest on the ~30s stats tick
  with `maxInferMs`/`maxGapMs` — **maxima, not averages**, `atomic.Int64`, gap
  measured in `enqueue`, and the first frame records NO gap.
- **No WALL-CLOCK timestamps from the device**: ages, or the MONOTONIC clock.
- **Thresholds must match**: `SetBargeThreshold` applies the lower bar while
  `PcmSpeaker.VoiceAudible(wakeword.ScoreSpan)` (not `IsStreaming`), and never
  raises it. `turns.wake_threshold` is the EFFECTIVE threshold; the rollup has
  agreed / missed / **not_comparable**.
- Correlation (`em_shadow.ShadowTracker`) at turn-persist time: nearest crossing
  within `MATCH_WINDOW_S` (2.0s) wins and is **consumed**. `turns.dev_shadow`
  = was it scoring; `wake_counters.dev_*` = crossings with no turn.
- Assets at `shadow.DefaultDir` (`/data/local/share/echomuse/oww`,
  `EM_OWW_DIR`), **never in the firmware** (would double the OTA). Absence is
  ordinary, logged once. `device/tools/oww_probe` checks parity and CPU.
  **Enable on one device at a time** (~23% of a core with
  `embedding_stream.onnx`, which falls back to the stock embedding).
- **Re-read the scorer PER FRAME, never cache it for a stream**, and `Close()`
  signals `quit`, never closes `ch` — either alone leaves a device deaf or
  panicking.
- **Bouncing the HA connection (`update_oww_model`) flaps EVERY entity** and
  fires state-triggered automations on the event entity (user fix `not_from:
  [unavailable, unknown]`). Think before adding a bounce.
- **Install-before-switch, never a fallback**: `em_oww_sync.hold_back_model`
  holds `owwModel` back until the classifier is on the device;
  `_install_then_switch` pushes the real config after. Never stand a device
  down to controller scoring meanwhile (silent override; streaming on private
  firmware). Only locally-scoring devices are held; old AND new mode are
  consulted; the install runs in the background; tests pin ordering,
  capability gate and failed-install-returns.
- **A missing model never changes the mode**, offline case included:
  `effective_mode` takes no readiness, and a test pins that the reconcile never
  assigns the mode.
- **Every device carries all four stock classifiers**
  (`em_oww_assets.STOCK_MODELS`, test-pinned both ways to `dashboard.jsx`
  `WW_MODELS`; `timer`/`weather` are intents, excluded). `desired_assets`
  (`include_stock` True) serves both transports. **`CLASSIFIER_SLOTS` budgets
  leftover CUSTOM classifiers only**; stock is never evictable.
- **Reconcile-on-connect** (`em_oww_sync.reconcile` via
  `em_reconcile.on_connect`): failure to LOOK is not absence; repair, never
  switch; full set whatever the mode (`reconcile_action`); quiet when nothing
  to do; **an install is followed by the config** (`push_config`); presence by
  **md5, not filename**. `missing_selected_classifier` = deaf (warn, repair,
  re-push); `missing_assets` = incomplete (repair quietly at info).

### Asset distribution (`em_oww_assets.py`)

`em_oww_assets` plans (pure); `em_api` executes over the wizard (USB/ADB,
**mandatory step**) or the shell plane.

- ARM runtime **vendored in the controller image**, pinned by AAR sha256
  (`onnxruntime-android` 1.19.2); models from openwakeword or `oww_models/`.
- **md5 is the only success**: push to `.part`, rename on match.
- **Four classifier slots, LRU by device mtime**; the selected model is pinned;
  only positively recognised classifiers are deleted.
- Free space checked against what needs sending, via `parse_free_mb`, never an
  awk field (busybox wraps).
- **`TransferResult` is truthy, not a list** — never assign it over an
  accumulator. Pinned by test.
- `DEVICE_DIR`, model names and the stem rule are test-pinned to the firmware.
- **`silero_vad.onnx`** (speech gate, `internal/client/speechgate.go`) is OUR
  typed-tensor copy (`controller/tools/silero_typed.py`, Dockerfile `silero`
  stage, sha256-pinned), never openwakeword's, which SIGBUSes ORT on armv7 and
  **presents as a hang** — look for `BUS_ADRALN` in `logcat`. Optional, never
  evictable, RMS gate without it.

## The external audio jack

- **`Init()` runs `stop media` and waits on the speaker substream before
  opening** (mediaserver holds it with a headset in; ALSA's open blocks
  forever). Opens anyway on timeout, logging the holder. **Never make `stop
  media` permanent** — mediaserver keeps the HAL, DSP and I2S clock alive; we
  win the race once per start. Hang tell: no `PcmSpeaker initialised`.
- **The speaker is card 0 device 23, the mic 24**; `pcm0p` is Android's.
- **`SetJackRouting` applies BOTH positions and stays idempotent**: amp
  switch, `HP Driver Gain Volume` (ctl 62, accdet drops it to 0), and
  `Audio_DacMux_Setting` On with a plug (in the 30s reconcile). `Right Channel
  Only` and `Ignore Ramp Up` deliberately not copied. Android's `AudioOut_2`
  also reconfigures codec state on insert.
- **Routing is PHYSICAL** (jack contacts): build no mux/destination layer.
  Level is ours.
- **Unresolved (#117, #141): a plug degrades the whole audio subsystem.** Read
  #117 and JOURNAL 2026-08-12 first; codec, SoC and mixer state are IDENTICAL
  audible vs silent — **do not look there again**. Open: whether
  `Ext_Speaker_Amp_Switch` gates the internal driver; only a **positive**
  `skew` shows lost audio (healthy devices drift negative).
- **No stereo**: the device duplicates L=R; the downmix is the controller's.
- `tinymix` and the codec regmap (`/sys/kernel/debug/regmap/2-0018/registers`)
  exist. **Never `tinyplay` while the server runs** (wedges the device).
- **MIXER CONTROL IDS ARE NOT STABLE ACROSS KERNELS — resolve by NAME, and let
  an absent name be loud.** All writes go through `internal/bindings/mixer`
  (`mixer_get_ctl_by_name`); `guard_test.go` fails on
  `exec.Command("tinymix"`; `start_server.sh` uses names
  (`controller/tests/test_mixer_names.py`). **Call only functions both
  devices' `libtinyalsa.so` export** (check with `llvm-nm -D`) — a missing
  symbol is a crash-loop and A/B rollback. `device/tools/mixer_probe` reads
  names through the same path. The mic ADCs have no regmap: prove capture by
  signal.

## The BLE proxy, and what it costs the device running it

Passive HCI scan forwarded to the controller as a second ESPHome device (recon
and cadence: `controller/CLAUDE.md`). **Scanning costs its own Echo's WiFi.**

### The LE scan costs the WiFi link, and the scan YIELDS for it (2026-09-23)

- Settled, do not repeat: the chip ignores scan interval/window (320/30 is just
  `esp32_ble_tracker`'s default); cost is proportional to scan time; vendor
  init, power save and 2.4GHz don't help — **do not rebuild the kernel for
  power save**. `coex_wmt_ant_mode=1` is right.
- **`Scanner.Yield`**, polled every 100ms in `cmd/server.go` (never edge-set),
  stops the scan for a button turn, a listening session, a voice reply arriving
  (`PcmSpeaker.VoiceArriving`, self-ending 2s after the last period) and any
  shell session. `/dev/stpbt` stays open; the silence watchdog is disarmed
  while yielded. A controller-scoring always-on stream is not a turn.
- **Music gets bursts (`bluetooth.MusicDuty`) sized from the buffer, never the
  clock** — a learned reserve; the music wins. Sendspin declares
  `bluetooth.MusicHeadroom` while the proxy is on. Tests: never dry, no gap past
  8s. Log `[ble] no scan for 8s of music` is the one to read.
- **Adverts go on `/data` as `0x06` only when the ack announced
  `ble_adverts_data`**, else the control message, which the controller keeps
  accepting forever.
- In `DataClient.SendBleAdverts`: **an unsendable batch is DROPPED**, never sent
  on the control plane; **nothing is sent during a BOUNDED TURN — the turn, not
  `micActive`** (`advertsYieldToTurn`). The plane is chosen **per batch**.

### The emission gate (`emit.go`) — derived from what HA reads, not from taste

Target ~one advert per device per second. Forward on: a new payload from a
**known** address; RSSI ±3dB (≤1 per 250ms); 1s of nothing for that payload;
global 30s ceiling. **A first sighting is NOT an arrival** (addresses rotate);
an early flush **resets the ticker**. Never `filter_duplicates=1` at the chip
(it throws away the RSSI Bermuda reads).

### The HCI transport resets, unresolved as of 2026-09-01

A mic stall comes first and WiFi fails BEFORE our `/dev/stpbt` reopen, so
`em_ble_proxy`'s "reopening re-initialises the radio" warning is a hypothesis
— fix that wording. **Gate memory pressure is RULED OUT.** The gate correlates
with no known mechanism; do not call it the cause.

## CPU topology, thermals and why `cpuPct` lies

- Quad-core A53; HPS (`/proc/hps/`) parks idle cores, so one CPU in
  `/proc/cpuinfo` is a power state. **`num_base_perf_serv` (floor) is raised to
  2 every start (`applyCoreFloor`)**. **Never write `cpu1/online`** — HPS
  silently re-parks it.
- **emOS board tuning**: `server platform-init` (once per boot,
  `start_server.sh`, emOS only) applies stock's thermal/hotplug values. Board
  identified POSITIVELY by idme `device_type_id`; every zone and cooler resolved
  before ANY write, else none; every value read back; unknown board keeps kernel
  defaults. The script greps for `EM_PLATFORM_INIT_V1` first (an old binary
  would start a second server). `thermal_budget` levels: written 0-based, read
  1-based.
- **`cpuPct` is a share of ONLINE capacity** — read it with `coresOnline`.
  `thermalCoreLimit` below `coresTotal` is the earliest throttling signal;
  `maxTempC` is the hottest of all zones.

## Volume / mute persistence

- **Volume is software (`PcmSpeaker.SetVolume`, after the chain, ramped one
  period); the DAC sits at unity 127** and only mutes around amp/stream
  changes. 0.5dB/step law, unity 127, level 0 silent; starts from 100 via
  `SetVolumeApply`; silent until told.
- **Never above codec unity**: `DEVICE_VOLUME_MAX`/`volumeMax` = 127 (pinned),
  conversion in **one** place (`em_volume.py`). `Ext_Amp_Gain` (ctl 13) is inert;
  `HP Driver Gain Volume` (ctl 62) is live but a taste call.
- Buttons step 4dB over `volumeButtonFloor` (47)…127; explicit `Set()` is not
  floored (HA 0.0 = silent); a press from below lands on the floor.
- `volumeButtonSound`: only on a button-made change after 100ms of quiet, never
  for HA sets; `VolumeCue` gets `speaker.VolumeGain(newLevel)` baked in once,
  bypasses EQ/chain; Up at ceiling replays, Down at floor is silent; presses
  replace the in-flight cue. Dashboard gates on `volume_cue`, not `wake_cue`.
- **Volume is state, not a setting**: no dashboard control,
  `em_config_sections.STATE_KEYS`, read-only on Status.
- **Volume persists controller-side** (`startupVolume`), restored by
  `Server.SeedVolume` on the **first config push per run only**; the
  connect-time `volume_state` is suppressed until seeded or changed locally.
- **Mute is device-sovereign** in `/data/local/etc/echomuse/state.json`,
  restored pre-connect. **The write is synced**: temp, `Sync`, rename, sync
  the directory — rename-only lost mute across a power cut. Unreadable boots
  unmuted. `sendspin/store.go`, `aec/persist.go`, `config/report.go` stay
  unsynced on purpose.

## LED priority system

- Scenes (`em_scenes.py`, `ledScene`). With `led_anim` the device **animates
  locally** (`internal/server/animator.go`). `meter` reads the **voice plane
  before the music mix**; `meter*` → `resolveMeter`, which clamps itself.
  `ttlSec`: 30s listening, 135s spinner (**coupled to `_fetch_tts_audio`'s 60s
  ×2 — move one, move the other**), `em_scenes.meter_ttl` per response; it is
  the dead-man. A newer spec or `leds` frame replaces atomically (generation
  counter). `listening: true` on listening frames drives the direction overlay.
  Mute red and volume cyan are device-local.
- Outcomes by rhythm, not colour, from `device.last_turn_outcome` (set in
  `_persist_turn` **and `_record_dropped_turn`**, consumed by
  `_leds_turn_end`); **`no_ha` alone uses colour** (orange), after a 600ms
  listening hold.
- **Start a device-local pulse ONCE per state** (`pulseKind`); phase from
  elapsed time (`pulsePhase`).
- Playback ring clears on `playback_stats` (`device.playback_done`), never a
  wall-clock estimate.
- System LEDs beat the direction arc (`ledMode`); suppressed paints still record
  `baseLEDs`.
  - **The ring belongs to the LINK STATE; mute yields to it** (`linkDown`,
    `suppressPaint`, truth-tabled).
  - **Action and volume buttons are inert while `linkDown`**, gated in `cmd`,
    not the evdev binding. **MUTE stays live** (hardware; inert would hand back
    a live mic).
  - **A 5s action hold asks to pair, before that gate** (`client.PairHold` →
    `StartPairing`); the closing release is swallowed; the 2-min window closes
    early when the credential files change.
  - **Three link rings, kept three**: orange pulse = no controller; orange
    alternating = refused (`errRefused`), fixed by holding the button; white
    pulse = pending. `held` survives redials.
  - **FireOS 6: the mute LED is Amazon's `amz_privacy`** (gpio444, boots
    unmuted, can enter privacy but never leave). `led/privacy.go` finds it by
    name; `server/privacy.go` reconciles after every press and at boot, always
    to muted. **Never read `power_button_state`** (blocks the console).
  - **Mute ring is device-sovereign**: controller writes recorded, not painted.
  - **Volume arc** holds 2s against animations, not against an action press
    (`CancelVolumeDisplay()`, no repaint); expiry → `paintBaseLEDs`. Physical
    presses only. Mute LED is sysfs gpio444 active-high, not Amazon's gpio445
    (`mute_button.go`).

## Where the serial comes from

`GetSerialNo` (and emOS `init.c`, same order): **`/proc/idme/serial`**, then
`getprop ro.serialno` (FireOS only), then `androidboot.serialno` on the cmdline
(only before Android's init strips it). **idme leads: the cmdline truncates on
FireOS 6's 32-bit kernel and is the only source that can be WRONG** — its use is
logged. **Non-printable ASCII is rejected** (`unknown-device` beats a wrong
key). `emos/init/serialcheck.c` covers both parsers. `/proc/idme` also has
`board_id`, `product_name`, `productid`, `device_type_id`, `alscal`,
`miccal.0`–`6`.

## The emOS console password

`consolePassword` is written to `/data/local/etc/echomuse/console.pw`
(`config.WriteConsolePassword`), never checked; emOS's init enforces it so the
console works without the firmware. Never undo:

- **A POINTER** — empty means "no password" and must reach the device (as
  `DuckDb`).
- **Written only when the content changes** (eMMC wear).
- **From the config handler in `control.go`**, not `OnConfigApplied`.
- **Temp file + rename** — a torn record reads as NO password.

Record `<iterations>:<salt hex>:<hash hex>`, hashed by the controller; design
in `controller/CLAUDE.md`.

## cgo dependency

SpeexDSP C source (AEC) is vendored in `device/internal/aec/`. `internal/bindings/mixer` links the device's own `libtinyalsa.so` (see the mixer section for the symbol rule). The compiler Docker image provides the ARM cross-toolchain. If adding new cgo dependencies, they must compile cleanly with the `echomuse-compiler` image against the FireOS 5 sysroot.
