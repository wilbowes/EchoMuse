# The Device ↔ Controller Interface

This is the contract a device binary implements to be driven by the EchoMuse
controller. It exists so a **new board** — the Echo Show 8 (`crown`) is the
first after the Echo Dot (`biscuit`) — can be built against a written
specification rather than by reading the Dot's source. The controller is
already board-agnostic; a new board is almost entirely a fresh set of hardware
**Bindings** behind this same wire protocol.

Terms in **bold** are used in their ordinary sense here (device, controller,
capability, plane); no external glossary is required. The authoritative
source for every message and field is the code cited inline; where this
document and the code disagree, the code wins and this document is the bug.

**Stability.** Each section below is marked STABLE or EXPECTED TO CHANGE.
STABLE means implement against it now. EXPECTED TO CHANGE means the shape
described is real and current, but a specific, already-decided change is
coming and a new implementer should not treat it as the permanent target —
follow the note in that section for what changes and what doesn't.

**Negotiation is one-directional, and that's a known limit, not an
omission.** The device announces what it implements; the controller reads
that and replies with `pending` or silence (`em_controller.py:2906`) — it
announces nothing back. That's sufficient while both halves ship from this
repo. It stops being sufficient the moment a third-party device wants to use
something conditionally on what a *specific controller version* supports,
and nothing in this document solves that yet.

## The two rules everything else serves (STABLE)

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

## The three planes (STABLE, discovery EXPECTED TO CHANGE)

Each device opens **three** WebSocket connections to the controller. The
controller is discovered by mDNS (`_emcontroller._tcp.local`); the device dials
out on all three.

**mDNS won't stay the only discovery path.** Static IP and DNS-based discovery
land alongside it (#106, #166) for routed and tunneled setups where mDNS
doesn't reach. mDNS stays as one option among several, not the sole mechanism
— a board implementer should treat "how do I find the controller" as
configurable, not hardcoded to `_emcontroller._tcp.local`.

| Path | Payload | Direction | Purpose |
|------|---------|-----------|---------|
| `/control` | JSON text | bidirectional | Registration, LEDs, mic start/stop, button/state events, config push, WiFi, shell control |
| `/data` | binary | bidirectional | Mic PCM in; speaker + music PCM out |
| `/shell/{device_id}` | raw binary | device-dialled on demand | Root shell proxy, opened only after a `shell_open` command |

All three exist in plain (`ws://`) and TLS (`wss://`) form; see
[Link auth & TLS](#link-auth--tls). The `/shell` plane is not dialled until the
controller asks for it.

## Registration and capabilities (STABLE)

**No namespace convention exists yet for capability strings.** All current
strings (`mic`, `speaker`, `leds`, ...) are short, unprefixed, and minted by
this repo. Nothing stops a third-party device from minting a new string that
later collides with one this project mints for something else — both sides
would silently ignore the mismatch, per the degrade rule above, and neither
side gets an error. Until a registry or vendor-prefix scheme exists, a
third-party capability should pick a name unlikely to collide (e.g. a
project-specific prefix) and expect it to be renamed later if this project
claims the same string for something else.

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

`capabilities` is the negotiation signal. The Dot announces nine unconditionally
plus one conditional (`capabilities()` in `control.go:738`):

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
| `ambient_light` | only if the sensor is actually readable (`als.Present()`) | Reports light readings |

**`oww_shadow` and `oww_trigger` are two capabilities, not one, and that split
is load-bearing.** Shadow shipped first, so there is firmware in the field that
scores and reports but has no code to act on a detection. A controller that
treated "can score" as "can trigger" would stand down its own wake detection
and wait for a trigger the device never sends — a device that scores perfectly
and never answers. A board that scores locally SHOULD announce both; a board
that only relays scoring announces `oww_shadow` alone.

The controller reads capabilities via `@property` gates on the `Device` class
(`controller/em_controller.py`): `led_anim_capable`, `audio_mix_capable`,
`button_hold_capable`, `oww_shadow_capable`, `oww_trigger_capable`, plus direct
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

## `/data` — binary frames (playback path EXPECTED TO CHANGE)

First byte is the frame type; the rest is the payload. **The type codes are
namespaced by direction** — `0x04`/`0x05` mean different things depending on who
sent them, and this is deliberate (`device/internal/client/data.go:25`). A
board implementer must not read a single global table.

**`0x01`–`0x05` are taken, in both directions. No range is reserved yet for a
third-party frame type.** Don't mint a new code above `0x05` and assume it's
free — treat this as unallocated space until a reservation scheme exists,
and check back here rather than guessing.

**The controller→device playback frames (`0x02`–`0x05`) are the fallback
path, not the target.** #334 moves the device to fetching its own audio from
the controller rather than having PCM pushed at it. Push isn't going away —
it stays permanently as the capability-gated fallback for a device that
doesn't implement fetch, no flag day — but new device work should target the
fetch model once it lands, not build further on push. Implement against
`0x02`/`0x03` today if that's what's specified for your board's MVP, but
don't treat them as the permanent playback contract.

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

## Config push — `ConfigMessage` (fields STABLE, output-chain ownership EXPECTED TO CHANGE)

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
saveUtterances
```

Not every field is acted on by the device. `saveUtterances`,
`wakeArbitrationMs`, and the `button*` timing keys are **controller-side
only** — used for config scoping, never applied on-device. `owwOnDevice` is
both controller-consumed (scoping) and device-acted where `oww_shadow` /
`oww_trigger` are announced.

**The output-chain keys (`eq*`, `limiter*`, `bassGuard*`) are
capability-dependent, not unconditionally controller-side.** That's true
today because output processing happens entirely controller-side before
audio reaches the wire — but 3.0.0 moves the output chain onto the device
(#243, #272) for boards that implement it. Read it as: controller-side for a
device *without* the relevant capability, device-acted for one *with* it.
Don't hardcode "these keys are never applied on-device" into a board binding
— check the capability instead. A new board only needs to implement the keys
relevant to hardware it actually has; unknown keys are ignored, which is the
correct degrade.

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

The `crown` bindings are built behind a Go build tag mirroring `server` — one
binary per board, chosen at compile time, no runtime "which board am I"
detection. The hardware inventory — ALSA cards/devices, formats, input-device
paths, autostart — lives in
[echo-show-8-hardware-map.md](echo-show-8-hardware-map.md); this section records
only what the *interface* commits to for MVP.

**Capabilities for MVP** (mostly a subset of the Dot's — `display` is crown-only, the Dot has no screen):

| Capability | crown MVP | Note |
|------------|-----------|------|
| `speaker` | yes | `card0,device0` → RT5616 (issue #5). Streams clean, but `Ext_Speaker_Amp_Switch` is inverted (`On`=silent, `Off`=audible) — same trap as checkers, confirmed by ear 2026-08-26. Must be driven `Off` in the binding's init; the boot default is `On` |
| `mic` | **proven** | `card0,device22`, 6ch/16kHz/`S24_3LE`, confirmed by capture on real hardware 2026-08-26 — real signal, not digital zeros. HW_REFINE matches checkers' driver constants exactly. Open: quiet-room/across-room SNR, not raw capture — see below |
| `buttons` | yes | Resolved by name (`gpio-keys` vol, action button, camera shutter) |
| `leds` | **yes** | No physical ring, but the string stays on: `StatusOverlay` (in `crown_launcher`) renders the listening-ring hint as a status strip along the screen's top edge, so the wake cue still has something to paint with |
| `led_anim` | no | No ring for a local animation engine to spin frames for |
| `display` | **yes** | Tells the controller this device has a screen so the dashboard can draw a screen-bodied device icon (`dashboard.jsx`'s `DeviceIcon`) — never inferred from the decorative model string |
| `audio_mix` | **yes** (2026-08-27) | Announced now that it's confirmed implemented — `pcm_speaker_crown.go` carries the same two-plane mixer as biscuit, just wasn't advertised; music ducks under a voice turn on real hardware, verified live |
| `ambient_light` | tbd | Only if a readable sensor is found |
| `oww_shadow` / `oww_trigger` | no (MVP) | MVP uses **controller-side** wake word |

**Audio ownership**: `crown` seizes the mic and speaker exclusively
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
signal on all 4 mic channels, no digital zeros. The driver's own HW_REFINE
range was confirmed to match checkers' constants exactly. Full
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
