# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

It holds the rules. The why behind them (incidents, measurements, worked
examples, dates) is in `docs/history/root.md` under the same headings; read
the matching section there before changing that area or a rule.

**Before diagnosing a fault, reopening a decision or changing a default, grep
`docs/history/` and `JOURNAL.md` for the symptom, setting or function name.**
The stories behind the rules are no longer loaded; this is how they are found.

## What this is

EchoMuse repurposes Amazon Echo Dot Gen 2 (FireOS 5 / Android 5.1, codename "biscuit") as an open-source voice assistant satellite:

- **`device/`** — Go binary that runs directly on the rooted Echo Dot
- **`controller/`** — Python asyncio WebSocket server that manages devices, runs wake word detection, and proxies to a voice pipeline
- **`oww_forge/`** — standalone Docker trainer for custom openWakeWord models (`oww_forge/README.md`). Prefer the published image (`forge-v*` → `ghcr.io/wilbowes/echomuse-forge`, `:latest` CUDA, `:latest-cpu`) to a local build; its Dockerfile's upstream pins must not move. `piper_voices.py`'s catalogue is **fetched, never hardcoded**.

Custom wake models upload via `/api/oww_models/upload` into `oww_models/` beside the DB, checked first by `em_oww_check` (loads in onnxruntime — not from aiohttp's `bytearray` — one float input `[N, T, 96]`, first output one score per frame, 0–1 on silence). Always score via `em_oww_models.prediction_key` (openwakeword keys by filename stem). Passing the check does not prove a model runs on an Echo's armv7 ORT: export a fixed `[1, 16, 96]` input, and put a new classifier architecture through `device/tools/oww_probe` on an Echo first.

## Who this file is for

It is checked in so that how the project is run can be seen (Wil, 2026-10-05),
and it is written for the maintainers' own sessions. If you are preparing a
contribution, or are an assistant working for someone who is:

- **The engineering rules apply to you**: capability negotiation, hardware by
  name, conforming to a spec and proving it, the listening rules.
- **The voice rules do not.** "Writing to people" and the
  `— Team EchoMuse (powered by Claude)` sign-off are for replies made on the
  project's behalf. Post as yourself. Never sign as Team EchoMuse, and never
  tell a user what the project will do.
- **Leave this file and the two directory-scoped ones out of your pull
  request.** We update them after a merge.

Written down because an assistant took the whole file as its own
instructions: five pull requests on 2026-10-04 were signed as the project,
a support reply on #712 read as an official answer, and #763 then rewrote the
sign-off rule itself.

## Where the detail lives

This file holds what is true across both halves. The depth sits in two
directory-scoped files, which load when you touch files in those trees — read
the relevant one before changing anything there.

- **`device/CLAUDE.md`** — building the firmware and the pinned compiler,
  the mic/audio pipeline, on-device wake word and asset distribution, the
  external audio jack, CPU topology and thermals, volume/mute persistence,
  the LED priority system, cgo.
- **`controller/CLAUDE.md`** — running the controller, the Home Assistant
  add-on and release channels, the ESPHome voice backend and HA entities,
  the output chain and ducking, schema migrations, config scoping, activity
  stats, support bundles, OTA, the emOS update, the provisioning wizard, the
  dashboard.

## Direction: portable, and not dependent on Amazon

**EchoMuse should run on more than one piece of hardware, with minimal change
per platform, and should not depend on Amazon's software to work.** Judge a
change by that. The Android surface in `device/` is about twenty call sites
(`tinymix`, `stop <service>`, `svc wifi`, `getprop`); the rest is ALSA, i2c,
evdev, sysfs and wpa_supplicant.

- **Prefer the Linux interface to the Android one**; isolate an unavoidable
  Android call rather than spread it.
- **Resolve hardware by NAME, not by number** — the wrong `event*` opens
  silently and leaves the buttons dead. Boards state their parts in
  `pkg/board` (`device/CLAUDE.md` → "Boards", `docs/boards.md`).
- **A change that makes a vendor blob necessary is going the wrong way**:
  at most a terminal opt-in for one platform. **Decline the direction, keep
  the findings** (PR #168).
- No Android build (LineageOS included) is the target: same vendor blobs.

Do not own the OS work, but do not prevent it: keep `pkg/led`, `pkg/mic`,
`pkg/speaker` and `pkg/buttons` honest as interfaces.

## Where a standard exists, conform to it and prove it

**If what we are handling has a known standard or spec, abide by the spec, and
have a test that proves it**, written from the spec's edges (min/max lengths,
every permitted character class, forbidden forms, published test vectors) and,
where possible, run against the real implementation. Worked example: #586.

**Never interpolate user-supplied text into a shell command** (serial console,
`adb shell`, `sh -c`). Send hex, base64 or a file.

## Writing to people: bottom line first

Anything a **person** reads leads with the answer and stays short — PR
comments, issue replies, review feedback, release notes. These go out on the
project's behalf to someone who did not sit through the reasoning, and Wil
sends many of them without following the internals: a reply he has to decode
before he can send it has failed, however accurate it is.

- **The first line answers it** — the verdict, the decision, or the ask.
  Everything after is support the reader is free to skip.
- **Three points, maximum.** Evidence is the *number*, not the derivation:
  "4.6–7.1% packet loss" rather than a paragraph on how it was measured.
- **Match the recipient.** A contributor who sent working code gets
  specifics; a user with a dead device gets what to do next; a passing
  question gets one line.
- **Cut** process narration, restating the person's own issue back at them,
  and hedging. That includes narrating your own structure or candour — "I'm
  going to make three points", "the point is", "here's where it gets
  interesting", "to be honest". Make the points.
- **Offer detail rather than pre-empting it.** One line does that.

**Five habits to spend sparingly** (derived from claudisms.ai, CC0). Generated
prose is recognisable by frequency rather than by any single phrase, so these
are budgets, not bans — except the third, which is a ban.

**The target is prose that does not read as stock, and authorship is not the
point.** Claude wrote most of this project and the record says so — commit
trailers, the `— Team EchoMuse (powered by Claude)` sign-off on every issue
reply. Nothing here is concealment, so do not write self-consciously to avoid
sounding like a model; that reads as strangely as the clichés do. Aim at clear.
A reader's objection is to stock phrasing, never to who typed it.

- **Say it; don't rate it.** No "this matters", "worth noting", "the most
  interesting part", "the right way". The reader decides what is important.
- **Plain verb before metaphor** — "is", "happens", "shows" ahead of "lives",
  "sits", "surfaces", "names", "carries". About one metaphor a page, where it
  does work a plain verb cannot.
- **Never contrastive negation**: "not X, it's Y", "not only X, but Y". Every
  published analysis of AI tells lands on this one first, and it is the
  easiest to write without noticing.
- **Vary sentence length.** Four short declaratives in a row is a tic; one
  short sentence after a long one is punctuation.
- **No corporate verbs**: leverage, unpack, double-click, surface.

**The exception is anything irreversible**, or anything asking someone to act
on their own hardware — an OTA, rooting, a schema migration, a partition
write. A truncated warning is how somebody bricks a device, so the caveat
stays whatever it costs in length.

**None of this applies to commit messages, this file, or code comments.**
Those are the record rather than correspondence, and their density is
load-bearing — the "why" written down here is what keeps a fixed bug fixed,
and most of this file exists because something was learned the expensive way.
Short where a person is being addressed; complete where something is being
recorded.

## Listening is private by default, and every claim about it is the Echo's own

**`docs/listening.md` is the spec; the wake word runs in one place per Echo.**
"On this Echo" (`owwOnDevice=on`, the default) sends nothing until the Echo's
own wake word fires, then only until end of speech; "On the controller"
(`off`) streams continuously and is labelled as streaming wherever it shows.
`shadow` is a developer diagnostic, not a user choice.

- **Privacy statements come from what the Echo REPORTS (`listen_state` →
  `em_listen.resolve`), never from configuration.** Unknown shows as unknown.
- **Nothing ever falls back to streaming.** An Echo that cannot run its own
  wake word is `degraded` (button only) and says why.
- **The device enforces its own limits** (ack timeout, max session length,
  mute, link loss), so no controller failure can leave an Echo streaming.

## Device/controller compatibility

Any device/controller pairing can occur in the field (`tests/test_capabilities.py`):

- **Negotiate by capability, never by version string.** The register message lists them (`internal/client/control.go`, `capabilities()`): `mic`, `speaker`, `leds`, `led_anim`, `buttons`, `oww_shadow`, `oww_trigger`, `button_hold`, `audio_mix`, `aec_hw_ref`, `oww_local_only`, `output_chain`, `wake_cue`, `volume_cue`, `remote_volume_arc`, `response_level`, `stock_sound`, `wake_mic`, `pairing`, `sendspin`, `ble_connect`, `wake_word_off`, and `ambient_light` only when the sensor is readable. A UI control whose feature is missing is **disabled with the reason**. `oww_shadow` (scores) and `oww_trigger` (acts) must stay two; without `audio_mix` keep the pause/resume path. When a capability cannot be proven at registration, report the runtime state separately (`aec_hw_ref` vs `aecRef` on stats) and gate UI on that.
- **Both ways**: the `ack` carries the controller's `features`. Adding a message is safe unnegotiated; MOVING one never is — the new path is silently discarded.
- **A static boot property rides REGISTER, not the stats tick, and is persisted** — its consumer (`em_reconcile.on_connect`) runs before the first tick. `base_os` (schema v21): absence means Android for payload gating, but must NOT disable a control.
- The controller sets the device clock via `time_ms` on the `ack`, stepped only past 30s; no NTP on the device.
- **Degrade to old behaviour, never to a wrong answer.** Unknown fields and messages are ignored; a missing measurement stores **NULL, not 0**.

## Versioning / releases

Three independent tag namespaces, each `git tag -a --cleanup=verbatim` (the
default strip deletes `#` heading lines from the notes):

- **Device** `v*` → `release.yml` → Release with a `server` asset; OTA compares the embedded tag to `firmware_ver` — don't change the scheme. EA is `vX.Y.Z-ea.N`, a prerelease that GA controllers skip by flag and name. Updates compare by version, never string (`version.firmware_update`).
- **emOS** `emos-v*` → `emos-release.yml` → `init` (aarch64) as its own asset, which must keep that name (`em_releases.fetch_emos`), and `init32` (armv7a) in `emos-payload.zip`; both static. **Only an init is ever published** — an image would redistribute Amazon's kernel. The `emos-v*` prefix keeps it out of `fetch_firmware` and stamps `/etc/os-release`. EA is `emos-vX.Y-ea.N`.
- **Controller** `controller-v*` → `controller-release.yml` → `ghcr.io/wilbowes/echomuse-controller` (amd64 + arm64). No GitHub Release: the annotation is the only copy of the notes. Newest by parsed version, never list order. The update notice is **advisory only** (`tests/test_deploy.py`).

**UAT gates GA, not EA**: full run (`tools/uat`, `docs/uat-results/<GA>.md`)
on FireOS 5, emOS 32-bit and emOS 64-bit; a point release gets targeted
checks in its notes instead. Echoes are named by their last four characters in
anything published.

**A critical GA fix ships from `release/X.Y.Z` off the GA tag**, never as an EA
(that would downgrade EA users): fix, pin and changelog only, the same fix on
main as its own PR, bench-checked on the UAT rig before tagging; main's
`controller/config.yaml` moves to it once the image exists.

**EA notes go in `controller-ea/CHANGELOG.md`, never the GA file.**

**Pipeline hygiene**: actions pinned by SHA, runners by release (never
`-latest`), base images by digest (`tests/test_workflow_pins.py`); workflows
default to `permissions: contents: read`; release workflows refuse a tag that
is not a version; the apt layer is keyed on the ISO week (`APT_SNAPSHOT`).
Nothing checks build attestations before OTA yet.

**The release workflow re-tags `:sha-<short>`, it does not build**, and refuses
a commit whose main build failed. Order: merge → `Controller Build (main)`
green on the merge commit → tag. If the tag raced the build,
`gh run rerun <id>`; never re-cut the tag. A commit touching neither
`controller/` nor `controller-ea/` needs a `workflow_dispatch` build first.

Version resolution: `controller/version.py` (`EM_CONTROLLER_VERSION` → `git describe --match 'controller-v*'` → `dev`); pass the build arg on a local build.

## Architecture

### Device → Controller protocol

**The wire contract is `docs/device-controller-interface.md`**; keep this a
summary. Each device opens three WebSockets: `/control` (JSON), `/data` (PCM,
`0x01` in, `0x02`/`0x03` out), `/shell/{device_id}` (opened on `shell_open`).
The controller is found by mDNS (`_emcontroller._tcp.local`).

### Device-link TLS + token auth

Plain on 8767, TLS on 8770. The cert's identity is the DNS SAN
`echomuse-controller`, never an IP. Certs are backdated and the device clamps
its clock to the build time (`BuildUnix`) — don't "normalise" either.

The device re-reads `/data/local/etc/echomuse/{ca.pem,token}` on every dial.
With a CA it dials **wss only** and never sends the token over plain; the only
exception is a pairing window.

`em_linkauth.decide`: a wrong token always rejects; a missing token is allowed
only until the device has presented it once (`devices.token_confirmed_at`). A
first token comes only from a human (wizard over USB, or an approval).
`/data` and `/shell` must follow the control connection. A token for an unknown
device is **ignored, not rejected**. Flip `REQUIRE_DEVICE_TLS=1` only when every
Link row is green `wss (TLS)`; re-pairing under it is USB only.

**Deleting a device closes its control plane** (`_delete_device`), after the
row is gone. `issue_credentials` writes to whoever holds the connection, so it
runs only behind an approval (`em_pairing`); a pairing device's token is
cleared before the auth decision, never bypassing it.

## Device config push

`config.ConfigMessage` (camelCase JSON) is sent on connect and on change.
Non-zero fields apply; zero/nil are ignored. No restart needed.

Keys: `consolePassword`, `vadThreshold`, `vadSpeechMs`, `vadSilenceMs`, `owwThreshold`, `owwModel`, `owwSpeexNs`, `adcDigitalGain`, `adcMicpga`, `micGainDb`, `startupVolume`, `beamAngle`, `beamformingEnabled`, `aecEnabled`, `aecDelayMs`, `aecTailMs`, `aecRefSource`, `agcEnabled`, `nsAsr`, `bargeInEnabled`, `bargeInThreshold`, `bleProxyEnabled`, `bleProxyConnections`, `eqBands`, `eqLoudness`, `limiterEnabled`, `limiterThreshold`, `limiterRelease`, `bassGuardEnabled`, `bassGuardDb`, `ledScene`, `ledListenColor`, `ledThinkColor`, `meterAttack`, `meterDecay`, `meterFloor`, `meterGamma`, `meterRef`, `meterCurve`, `wakeArbitrationMs`, `duckDb`, `buttonSingleTapEvent`, `buttonMultiTapMs`, `wakeSound`, `wakeSoundLevel`, `volumeButtonSound`, `remoteVolumeArc`, `responseLevel`, `wakeMic`, `wakeClipCapture`, `wakeClipMinScore`, `controllerEndpoints`, `sendspinEnabled`, `sendspinUnpaired`, `sendspinName`, `wakeWordEnabled`, `owwOnDevice`, `saveUtterances`.

`wakeWordEnabled` and `consolePassword` are **pointers** — false and empty are
meaningful. The device ignores `saveUtterances`, `wakeClipCapture`,
`wakeClipMinScore`, `wakeArbitrationMs`, `button*` and `controllerEndpoints`
(fleet-only, delivered as `controller.json`; the fleet sync removes only a file
it wrote). Output-chain keys apply only when both sides announce `output_chain`.
`responseLevel` is capped so gain × volume never exceeds unity.

## Build and test quickref

```bash
git submodule update --init          # GoTinyAlsa fork — see device/CLAUDE.md
cd device && ./compile.sh            # needs the echomuse-compiler image
cd device && go test ./...
cd controller && python -m pytest tests/   # needs: pytest numpy scipy pyyaml
cd emos/init && cc -O2 -o /tmp/ringsim ringsim.c -lm && /tmp/ringsim --check
cd emos/init && cc -O2 -o /tmp/pwcheck pwcheck.c && /tmp/pwcheck
cd emos/init && cc -O2 -o /tmp/tmoutcheck tmoutcheck.c && /tmp/tmoutcheck
cd emos/init && cc -O2 -o /tmp/wpacheck wpacheck.c && /tmp/wpacheck
cd emos/init && cc -O2 -o /tmp/cmdlinecheck cmdlinecheck.c && /tmp/cmdlinecheck
cd emos/init && cc -O2 -o /tmp/serialcheck serialcheck.c && /tmp/serialcheck
cd emos/init && cc -O2 -o /tmp/trialcheck trialcheck.c && /tmp/trialcheck
cd emos/init && cc -O2 -o /tmp/devcheck devcheck.c && /tmp/devcheck
sh emos/device/em-wifi-check.sh
```

CI runs both suites, `go vet`, a headless-Chromium walk of every dashboard tab
(`uat/`), and `test_design_tokens.py` for CSS the browser drops silently.

**A source guard finds code through `tests/_sources.py`** (`MODULES`,
`find_def`, `code_only`), never by reading one file by name — else moved code
silently passes the guard.

**Three ratchets keep the module split**: `tests/test_layering.py` (only
em_controller imports em_api; `em_routes_*` only from em_api and each other;
base modules never reach upward), `tests/test_size_ratchet.py` (modules over
1,500 lines or functions over 150 may not grow; `controller/tools/size_ratchet.py
--update` after a shrink; a raise goes in the same change), and
`tests/coverage_floor.json` (never lowered; a new module needs a floor).

**emOS's off-target tools ARE its suite**: each `#include`s `init.c` and drives
the real functions. Add one wherever a wrong answer is SILENT on hardware. CI
asserts the init is static — a dynamic PID 1 prints nothing at all.
