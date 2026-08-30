# The Device ↔ Controller Interface

This is the contract a device binary implements to be driven by the EchoMuse
controller. It exists so a **new board** — the Echo Show 8 (`crown`) is the
first after the Echo Dot (`biscuit`) — can be built against a written
specification rather than by reading the Dot's source. The controller is
already board-agnostic; a new board is almost entirely a fresh set of hardware
**Bindings** behind this same wire protocol.

Terms in **bold** are defined in [CONTEXT.md](../CONTEXT.md). The authoritative
source for every message and field is the code cited inline; where this
document and the code disagree, the code wins and this document is the bug.

## The two rules everything else serves

Both are guarded by `controller/tests/test_capabilities.py`. A board that
honours them can pair with any controller version, and vice versa.

- **Negotiate by capability, not version.** The device announces what it
  implements in its `register` message; the controller reads
  `Device.capabilities` and gates each feature on the relevant string. Never
  compare version strings — that puts release history in the controller and
  misjudges dev builds. A UI control whose capability the device lacks is shown
  **disabled with the reason**, never as a control that silently does nothing.
- **Degrade to old behaviour, never to a wrong answer.** Unknown JSON fields
  and message types are ignored in both directions. Where a new field records a
  measurement, absence stores as **NULL, not 0** — a device that cannot report
  a thing must not read as having reported zero.

## The three planes

Each device opens **three** WebSocket connections to the controller. The
controller is discovered by mDNS (`_emcontroller._tcp.local`); the device dials
out on all three.

| Path | Payload | Direction | Purpose |
|------|---------|-----------|---------|
| `/control` | JSON text | bidirectional | Registration, LEDs, mic start/stop, button/state events, config push, WiFi, shell control |
| `/data` | binary | bidirectional | Mic PCM in; speaker + music PCM out |
| `/shell/{device_id}` | raw binary | device-dialled on demand | Root shell proxy, opened only after a `shell_open` command |

All three exist in plain (`ws://`) and TLS (`wss://`) form; see
[Link auth & TLS](#link-auth--tls). The `/shell` plane is not dialled until the
controller asks for it.

## Registration and capabilities

Immediately after the `/control` socket opens, the device sends one `register`
message (`device/internal/client/control.go`):

```json
{
  "type": "register",
  "device_id": "<stable id>",
  "version": "<firmware version, from build ldflags>",
  "capabilities": ["mic", "speaker", ...],
  "ip": "<local ip, omitted if 127.0.0.1 or unresolved>",
  "ambient_light_status": { "...": "..." }
}
```

`capabilities` is the negotiation signal. The Dot announces ten unconditionally
plus one conditional (`capabilities()` in `control.go`):

| Capability | Condition | Meaning |
|------------|-----------|---------|
| `mic` | always | Streams mic PCM on `/data` |
| `speaker` | always | Plays speaker PCM from `/data` |
| `leds` | always | Accepts `leds` frames |
| `led_anim` | always | Renders animations locally from one `led_anim` spec per state change (vs. controller-streamed frames) |
| `buttons` | always | Emits `button` events |
| `oww_shadow` | always | Can score the wake word locally and **report** (never act) |
| `oww_trigger` | always | Can **act** on its own wake detection — kept separate from `oww_shadow` on purpose (see below) |
| `button_hold` | always | Emits long-press (`heldMs`) |
| `audio_mix` | always | Holds music on its own frame types and mixes it under voice rather than pausing |
| `aec_hw_ref` | always | Can take the AEC far-end reference from a playback loopback in the mic capture itself, and falls back to the software tap at the ALSA write when the board has none |
| `ambient_light` | only if the sensor is actually readable (`als.Present()`) | Reports light readings |

**`aec_hw_ref` is a capability with a runtime companion, and both are needed.**
The capability says the firmware knows *how* to use a hardware echo reference.
Whether the board actually has one is answered by `aecRef` on the periodic
`stats` message — `"hw"`, `"sw"` or `"off"`, and **absent** from firmware too
old to say, which must not be read as `"sw"`.

The split exists because the proof is only available at runtime: a channel is
confirmed as a playback loopback by being bit-exact silent at idle *and*
carrying audio while the speaker plays, and nothing has played at registration
time. It matters for the AEC delay control, which compensates write-to-ear
latency for the software tap and means nothing on a frame-aligned hardware
reference. Gating that control on the capability would grey it out on every
current device, including those that fall back to the tap and need it; gating
it on `aecRef == "hw"` disables it exactly where it does nothing.

A board with no loopback announces `aec_hw_ref` and reports `aecRef: "sw"`
forever, which is the correct degraded behaviour and needs no controller
change.

**`oww_shadow` and `oww_trigger` are two capabilities, not one, and that split
is load-bearing.** Shadow shipped first, so there is firmware in the field that
scores and reports but has no code to act on a detection. A controller that
treated "can score" as "can trigger" would stand down its own wake detection
and wait for a trigger the device never sends — a device that scores perfectly
and never answers. A board that scores locally SHOULD announce both; a board
that only relays scoring announces `oww_shadow` alone.

The controller reads capabilities via `@property` gates on the `Device` class
(`controller/em_controller.py`): `led_anim_capable`, `audio_mix_capable`,
`button_hold_capable`, `oww_shadow_capable`, `oww_trigger_capable`,
`aec_hw_ref_capable`, plus direct
membership checks for the base four (`mic`/`speaker`/`leds`/`buttons`). Every
string the controller checks must be one the device can send, and every string
the device sends must be one the controller understands — both directions are
pinned by `test_capabilities.py`, because a typo on either side fails silently.

## `/control` — JSON messages

Dispatched on the device by the type switch in `control.go`. Unknown types are
ignored (forward-compatibility). Payload fields below are the ones acted on;
absent optional fields take prior/default behaviour.

**Device → Controller**

| `type` | Payload | Meaning |
|--------|---------|---------|
| `register` | see above | Handshake, sent once at connect |
| `button` | `clickType`, `down`, `heldMs`, `muted`, `button.type` | Button press/release; `heldMs` only if `button_hold` |
| `mute_state` | `muted` | Mute toggled (mute is device-sovereign — see `device/CLAUDE.md`) |
| `volume_state` | `level` | Volume changed; controller persists it as `startupVolume` |
| `oww_shadow_cross` | score/threshold/age fields | Shadow-mode wake crossing (report only) |
| `oww_wake` | score, effective threshold, age | On-device trigger fired (`owwOnDevice=on`); lands in `Device.pending_wake` |
| `ambient_light` | `value` | Light reading (only if `ambient_light`) |
| `pong` | — | Keepalive reply |

**Controller → Device**

| `type` | Payload | Meaning |
|--------|---------|---------|
| `leds` | `leds[]`, `listening?` | One LED frame; `listening:true` marks the listening ring so the direction overlay keys off it |
| `led_anim` | `{pattern, colors, periodMs, ttlSec}` | Local animation spec; sent only if `led_anim` |
| `mic_start` | `lock_mic?` | Start mic stream. `lock_mic:false`/absent = always-on ungated wake stream; `true` = bounded, VAD-gated turn |
| `mic_stop` | — | Stop the mic stream |
| `beam_lock` / `beam_unlock` | — | Lock beamformer to the chosen perimeter mic for a turn / return to omni |
| `volume_set` | `level` | Set absolute volume |
| `duck` | `on` | Duck music under a voice turn (turn start/end) |
| `config` | `ConfigMessage` fields | Push configuration (see below) |
| `wifi_change` / `wifi_commit` | `ssid`,`psk` / — | Switch WiFi with auto-rollback; commit finalises |
| `shell_open` / `shell_close` | `pty?` | Ask the device to dial `/shell` (`pty:true` = interactive) / close it |
| `music_flush` / `speaker_flush` | — | Flush the music / voice buffer (barge-in uses `speaker_flush`) |

## `/data` — binary frames

First byte is the frame type; the rest is the payload. **The type codes are
namespaced by direction** — `0x04`/`0x05` mean different things depending on who
sent them, and this is deliberate (`device/internal/client/data.go:25`). A
board implementer must not read a single global table.

**Controller → Device (playback)**

| Code | Name | Meaning |
|------|------|---------|
| `0x02` | speaker | Voice-response PCM chunk; played immediately |
| `0x03` | speaker EOS | End of voice stream |
| `0x04` | music | Music PCM chunk on its own plane — **only if `audio_mix`** |
| `0x05` | music EOS | End of music stream — **only if `audio_mix`** |

**Device → Controller (capture)**

| Code | Name | Meaning |
|------|------|---------|
| `0x01` | mic | Mic PCM chunk (mono `S16_LE`, 16 kHz) |
| `0x04` | VAD-end | Bounded turn: speech was detected, then ended |
| `0x05` | no-speech-timeout | Bounded turn: no speech ever detected before the timeout |

`0x04`/`0x05` are safe to reuse because playback frames only ever flow to the
device and capture frames only ever flow from it. The two capture sentinels are
distinct on purpose: "wake word then silence" (quietly give up) must be
distinguishable from "spoke, and the backend had nothing to say."

Mic stream shapes (`device/CLAUDE.md`, Device audio pipeline):

- **Always-on wake stream** (`mic_start` without `lock_mic`) is ungated and
  AGC-free — every period is sent continuously so the wake scorer sees an
  uninterrupted stream.
- **Bounded turn stream** (`lock_mic:true`) is VAD-gated with a preroll ring,
  ends with a `0x04` sentinel when the gate closes after speech, and ends with
  `0x05` if no speech arrived within the timeout.

## Config push — `ConfigMessage`

The controller sends `config` on connect and on any per-device config change.
Fields are camelCase (`device/internal/config/config.go`). **Partial update:
non-zero/non-nil fields are applied; zero/nil fields are ignored**, so pointer
types (`*bool`, `*int`) exist where `false`/`0` must be distinguishable from
"unset." Changes take effect immediately — no restart.

The canonical field set (repo-root `CLAUDE.md`, "Device config push"):

```
vadThreshold, vadSpeechMs, vadSilenceMs,
owwThreshold, owwModel, owwSpeexNs, owwOnDevice,
adcDigitalGain, adcMicpga, micGainDb,
startupVolume,
beamAngle, beamformingEnabled,
aecEnabled, aecDelayMs, aecTailMs, agcEnabled, nsAsr,
bargeInEnabled, bargeInThreshold,
bleProxyEnabled,
eqBands, eqLoudness, limiterEnabled, limiterThreshold, limiterRelease,
bassGuardEnabled, bassGuardDb,
ledScene, ledListenColor, ledThinkColor,
meterAttack, meterDecay, meterFloor, meterGamma, meterRef, meterCurve,
wakeArbitrationMs, duckDb,
buttonSingleTapEvent, buttonMultiTapMs,
owwOnDevice, saveUtterances
```

Not every field is acted on by the device. The output-chain keys (`limiter*`,
`bassGuard*`), `eq*`, `saveUtterances`, `wakeArbitrationMs`, and the `button*`
timing keys are **controller-side** — that processing happens before the audio
reaches the wire, or is used only for config scoping. `owwOnDevice` is both
controller-consumed (scoping) and device-acted. A new board only needs to
implement the keys relevant to hardware it actually has; unknown keys are
ignored, which is the correct degrade.

## Link auth & TLS

- All three planes carry an `X-EM-Token` header, read from the device's
  credential file on **every dial** (`device/internal/client/tlscreds.go`), so
  a pushed credential takes effect on the next reconnect without a restart.
- TLS is selected when the device has a CA on disk **and** the controller
  advertises a `tls_port` mDNS TXT record → dial `wss://`. CA present but no TXT
  → plain with a warning (deliberate rollout fallback). The server identity is
  the fixed DNS SAN `echomuse-controller`, never an IP.
- Certs are backdated/long-lived **and** the device clamps its verification
  clock to the firmware build time, because an Echo boots with a bogus clock
  pre-NTP and a device that cannot connect cannot fix its clock. A new board
  inherits this — do not "normalise" either half.
- Enforcement is `em_linkauth.decide` (`controller/em_linkauth.py`): a wrong
  token always rejects; a stored token with none presented is allowed (the
  credential push itself rides the plain plane); a token for a device with
  nothing on record is ignored, not rejected. `REQUIRE_DEVICE_TLS=1` makes
  TLS+token mandatory.

## What the device binary owns

For any board, the device binary is the local hardware agent for a voice
satellite. Its responsibilities:

- **Voice assistant capture** — mic streaming (wake stream + bounded turns),
  the on-device capture pipeline (beamform/gain/AEC/AGC/VAD as the hardware
  supports), and optionally on-device wake scoring/triggering.
- **Playback** — speaker output, and music on its own plane mixed under voice
  (ducking) if `audio_mix` is announced.
- **Buttons** — press/hold/mute events, resolved by device **name**, not by
  index (`event2` is a button on one board and a touchscreen on another).
- **Announcements** — playing controller-pushed audio outside a turn.
- **Status output** — LEDs on the Dot; on a screenless-or-borrowed-screen board,
  whatever minimal status surface that board defines (or none).

Everything a backend can reasonably drive — intents, TTS, HA entities — stays
in the controller / Home Assistant via the ESPHome Voice Assistant API. The
device does not reimplement it, and does not depend on Amazon HALs or libraries
to do its own job (see the direction note in the repo-root `CLAUDE.md`).

## Board profile — `crown` (Echo Show 8, 1st gen)

The `crown` bindings are built behind a Go build tag mirroring `server`
(ADR-0001). The hardware inventory — ALSA cards/devices, formats, input-device
paths, autostart — lives in
[echo-show-8-hardware-map.md](echo-show-8-hardware-map.md); this section records
only what the *interface* commits to for MVP.

**Capabilities for MVP** (subset of the Dot's):

| Capability | crown MVP | Note |
|------------|-----------|------|
| `speaker` | yes | `card0,device0` → RT5616 (issue #5). Streams clean, but `Ext_Speaker_Amp_Switch` is inverted (`On`=silent, `Off`=audible) — same trap as checkers, confirmed by ear 2026-08-26. Must be driven `Off` in the binding's init; the boot default is `On` |
| `mic` | **proven** | `card0,device22`, 6ch/16kHz/`S24_3LE`, confirmed by capture on real hardware 2026-08-26 — real signal, not digital zeros. HW_REFINE matches checkers' driver constants exactly. Open: quiet-room/across-room SNR, not raw capture — see below |
| `buttons` | yes | Resolved by name (`gpio-keys` vol, action button, camera shutter) |
| `leds` / `led_anim` | **no** | No LED ring; a "voice turn" status overlay on the display is deferred past MVP and even then stays out of the user's way |
| `audio_mix` | later | Not required for the MVP voice loop |
| `ambient_light` | tbd | Only if a readable sensor is found |
| `oww_shadow` / `oww_trigger` | no (MVP) | MVP uses **controller-side** wake word |

**Audio ownership** (ADR-0002): `crown` seizes the mic and speaker exclusively
while EchoMuse runs, exactly like the Dot. The mic is **held continuously** —
MVP wake word is controller-side, so the device streams mic PCM the whole time
or it goes deaf to the next wake word. The **speaker** is grabbed for a turn
(the reply plus any media the turn asked for) and released to idle — the Alexa
model. The screen belongs entirely to the user's own software throughout.

**The mic was the MVP blocker; it isn't any more.** The mics sit on external
TLV320AIC3101 dies (TDM, `card0,device22`, 6ch/16 kHz/`S24_3LE`) — the same
format the Dot already captures (`device/internal/bindings/mic/pcm_microphone.go`,
9ch/16000 Hz/`tinyalsa.PCM_FORMAT_S24_3LE`), just fewer channels, same
`Channels * 3` stride, same `DeviceConfig`-driven path downstream. Proven on
real hardware 2026-08-26 with `device/tools/capture_mics`: opens clean, real
signal on all 4 mic channels, no digital zeros. `device/tools/hw_refine_probe`
confirms the driver's own range matches checkers' constants exactly. Full
history (why "digital zeros" was the original wrong read, the gain-fix
investigation, the actual capture data) is in
[echo-show-8-hardware-map.md](echo-show-8-hardware-map.md#on-hardware-capture-2026-08-26--the-gono-go-measurement-done).

**Still open: quiet-room/across-room SNR**, not raw capture — today's test was
a loud-room sanity check with music playing, not a wake-reliability
measurement. Speaker (#5) and mic (#6) are both proven now; SNR is the
remaining go/no-go for the MVP milestone (boot → connect → HA → wake → Assist
→ spoken reply).

Applies to every crown binding, not just mic: resolve input devices and i2c
addresses **by name**, never by number — `eventN` numbering isn't stable
across boards (it's the volume button on `biscuit`, the touchscreen on
`checkers`), and a wrong-number open fails silently rather than erroring. See
also issue #36 (Echo Show 5 / `checkers`) — same shape of bring-up problem,
worth reading before writing crown-specific bindings.

## Invariants (what the tests pin)

`controller/tests/test_capabilities.py` is the guard rail for this contract:

- The device announces the expected baseline capabilities.
- Every capability string the controller checks is one the device can send, and
  vice versa (bidirectional typo guard).
- `oww_shadow` and `oww_trigger` stay separate.
- Pending capabilities reported before the ESPHome server exists are not lost.
- A capability change bounces the HA connection so the entity list refreshes.
- A disabled UI control does not silently write when its capability is absent.

A board or protocol change that breaks one of these must update the test with
the reason, not route around it.
