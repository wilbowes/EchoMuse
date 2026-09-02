# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

EchoMuse repurposes Amazon Echo Dot Gen 2 (FireOS 5 / Android 5.1, codename "biscuit") as an open-source voice assistant satellite. Two components:

- **`device/`** — Go binary that runs directly on the rooted Echo Dot
- **`controller/`** — Python asyncio WebSocket server that manages devices, runs wake word detection, and proxies to a voice pipeline
- **`oww_forge/`** — standalone Docker batch trainer for custom openWakeWord models (synthetic TTS positives → augmentation → classifier head → `.onnx`). Not part of the controller; see `oww_forge/README.md`. **Published as an image** since 2026-08-20 (`forge-v*` tags → `forge-release.yml` → `ghcr.io/wilbowes/echomuse-forge`, CUDA on amd64 as `:latest` and CPU multi-arch as `:latest-cpu`) — prefer it to a local build, because the pins below are only preserved by a published artifact. Upstream pins in its Dockerfile are load-bearing (piper-sample-generator v2.0.0 flat layout; openWakeWord SHA with a `--convert_to_tflite` argparse patch). **Extra voices come from `piper_voices.py`, and its catalogue is FETCHED, never hardcoded** — 55 languages, ranked by speaker count, because a baked-in list of English voices makes every other language a code change; the same module backs the phrase preview. `google_tts.py` is rate-limited by Google at any real concurrency, so it retries transient failures and only retires a voice on a permanent refusal. Models install via the dashboard (Config → Wake word → "+ Custom model" → `/api/oww_models/upload`) into `oww_models/` beside the SQLite DB; `owwModel` stores the file path for custom models. openwakeword keys predictions by filename *stem*, never the path — always score via `em_oww_models.prediction_key`

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
  stats, support bundles, OTA, the provisioning wizard, the dashboard.

## Direction: portable, and not dependent on Amazon

**EchoMuse should run on more than one piece of hardware, with minimal change
per platform, and should not depend on Amazon's software to work.** That is
the direction, stated 2026-08-18. It is written here because contributors have
sent multi-thousand-line PRs without knowing which project they were
contributing to, and the answer changes how a change should be judged.

**The dependency is already thin, and keeping it thin is the job.** The entire
Android-specific surface in `device/` is about twenty call sites: `tinymix`
(×10), `stop <service>` (×6), `svc wifi` (×2), `getprop` (×2). Everything else
— mic, speaker, LEDs, buttons, ambient light, jack detect, WiFi state — is
ALSA, i2c, evdev, sysfs and wpa_supplicant. This is a Linux daemon that
happens to be running on Android because that is what shipped on the box.

Three consequences for reviewing a change:

- **Prefer the Linux interface to the Android one**, and where an Android call
  is unavoidable, isolate it rather than spread it.
- **Resolve hardware by NAME, not by number.** `event2` is the volume button
  on biscuit and the *touchscreen* on checkers; opening the wrong one succeeds
  silently and leaves the buttons dead. The same rule already applies to i2c
  (`als.resolve()` matches `tsl2540` by name, since `0-0039` is an
  enumeration accident).
- **A change that makes a vendor blob load-bearing is going the wrong way**,
  and needs to justify itself as a terminal opt-in for one platform rather
  than as the path forward. PR #168 (native AFE) is the **worked example,
  declined 2026-08-21**: opt-in per device, default off, old path untouched,
  built on genuine reverse engineering of the ASP pipeline, and audibly
  better — and still the wrong direction, because it made Amazon's audio HAL
  the path the audio takes. **Decline the direction, keep the findings.** Two
  live bugs it surfaced were fixed on main first (the DAC clipping above
  unity gain, the `Toggle` `disabled` prop), and stock's playback EQ was read
  off a device as coefficients rather than adopted as a binary (#247). We do
  not need Amazon's code to hit Amazon's target, and that is the general
  answer whenever a vendor blob looks like the shortcut.

**LineageOS is probably the wrong target; postmarketOS already has an
`amazon-biscuit` port** (its wiki and pmaports kernel config were corroborating
sources for the ALS second-source diagnosis — see JOURNAL 2026-08-11). There is
no Lineage port for a 2015 MT8163 on Android 5.1, and building one would mean
keeping the same MediaTek vendor blobs — swapping Amazon's Android for
somebody else's without removing the dependency.

The posture is therefore **not to own the OS work, but not to prevent it**:
keep `pkg/led`, `pkg/mic`, `pkg/speaker` and `pkg/buttons` honest as
interfaces, and treat each Android call site as something to isolate. Nothing
here commits the project to shipping a distro.

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
  and hedging.
- **Offer detail rather than pre-empting it.** One line does that.

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

## Device/controller compatibility

The two halves version independently, so any pairing can occur in the field. Two rules, both guarded by `tests/test_capabilities.py`:
- **Negotiate by capability, not version.** The device announces what it implements in its register message (`internal/client/control.go`, `capabilities()`: `mic`, `speaker`, `leds`, `led_anim`, `buttons`, `oww_shadow`, `oww_trigger`, `button_hold`, `audio_mix`, `aec_hw_ref`, and `ambient_light` **only when the sensor is actually readable**); the controller reads `Device.capabilities` via properties like `led_anim_capable` / `oww_shadow_capable`. Never compare version strings — that puts release history in the controller and misjudges dev builds. A UI control whose feature the device lacks is shown **disabled with the reason**, never as a control that silently does nothing.
  **`oww_shadow` and `oww_trigger` are two capabilities and must stay two.** Shadow shipped first, so there is firmware in the field that scores and reports but has no code to act — reading "can score" as "can trigger" stands the controller's own detection down and waits for a trigger that never comes, which presents as a device that scores perfectly and never answers. Same reason `audio_mix` is announced rather than assumed: without it the controller must keep the pause/resume path, because a device that cannot mix simply never plays the `0x04` stream.
  **`aec_hw_ref` is the shape to copy when a capability cannot be proven at registration.** It says the firmware knows how to take the AEC far-end reference from a playback loopback in the mic capture; whether the board HAS one is answered separately by `aecRef` (`"hw"`/`"sw"`/`"off"`) on the stats report, because confirming a loopback needs the speaker to have played and nothing has at register time. Same "could it" vs "is it" split as `oww_shadow` against `shadow.active`. Gate UI on the runtime value, not the capability: the AEC delay control is meaningless on a frame-aligned reference but essential to a device that fell back to the software tap, and both announce the capability.
  **Negotiation runs BOTH ways, and the controller's half is newer.** The `ack` carries `features` — the controller's own capability list, read exactly as the device's is: a feature that is absent is one the controller cannot do. It exists because `ble_adverts` moved from the control plane to `0x06` on the data plane (#404), and a device sending that frame to a controller which cannot read it loses every advertisement in **silence**, since unknown frame types are ignored. That is the general hazard whenever a message MOVES rather than being added: the old path stops being used and the new one is discarded, and nothing at either end reports it. Adding a message is safe unnegotiated; moving one never is.
- **Degrade to old behaviour, never to a wrong answer.** Unknown JSON fields and message types are ignored both ways. Where a new field records a measurement, absence stores as **NULL, not 0** — old firmware reporting no `playback_stats` must not read as "zero underruns", and a device that cannot score wake words locally must not read as "scored and missed" (hence `turns.dev_shadow` alongside `dev_wake_score`).

## Versioning / releases

Device firmware and controller are versioned independently from the same repo:

- **Device**: plain `v*` tags (e.g. `v2.7.6`) → `release.yml` → GitHub Release with the `server` binary asset. The tag is embedded in the binary and compared against `firmware_ver` by OTA — don't change this scheme.
- **Controller**: `controller-v*` tags (e.g. `controller-v2.8.0`) → `controller-release.yml` → Docker image pushed to `ghcr.io/wilbowes/echomuse-controller` (`X.Y.Z` + `latest`, CPU-only, **multi-arch: linux/amd64 + linux/arm64** — it said amd64 here until 2026-08-13, long after arm64 shipped). **No GitHub Release is created** — the OTA system's release polling (`em_api._fetch_latest_release`) filters for `v*` tags with a `server` asset, but controller releases stay out of the releases list entirely by design. **Tag controller releases with `git tag -a --cleanup=verbatim` too**: with no Release behind them, the annotation is the *only* copy of the notes, and it is what the dashboard's controller-update notice displays (`em_api._fetch_controller_release` reads it via `git/matching-refs` + the tag object). A lightweight controller tag ships an image nobody can read a changelog for. Pick the newest tag by **parsed version, never list order** — the refs API sorts lexically and returns `controller-v2.9.0` *after* `controller-v2.10.0`.

  The notice is **advisory only and must stay that way** (`tests/test_deploy.py` enforces GET-only + no mutating call in the banner): the controller is the user's container, updated with their own `docker compose pull`. An in-app update would restart the process serving the page, mid-request, with no way to report the outcome. Note a locally-built image defaults `EM_CONTROLLER_VERSION` to `dev`, which resolves to `unknown` and correctly shows nothing — pass `--build-arg EM_CONTROLLER_VERSION=$(git describe --tags --match 'controller-v*')` for a local build that knows what it is. Version comparison lives in `version.py` (`parse`/`compare`) so it is unit-testable without aiohttp; a build between tags parses **equal** to its tag and is ahead, not behind.

**The release workflow does NOT build — it re-tags the image the main build
already published for that commit.** `controller-release.yml` looks for
`:sha-<short>` and fails with "No image published for this commit" if
`Controller Build (main)` has not finished. So the order is **merge → wait for
`Controller Build (main)` to go green on the merge commit → then push the
tag**, and a tag pushed seconds after a merge fails on a race rather than on
anything being wrong. Hit on 2026-08-28 cutting `2.22.0-ea.4`: the build had
started 23 seconds earlier and the release checked while it was still pushing.
The recovery is only `gh run rerun <id>` once the build finishes — the tag,
the commit and the annotation are all fine and must not be re-cut.

**`--cleanup=verbatim` is not optional if the notes use Markdown headings.**
`git tag -a` defaults to `--cleanup=strip`, which treats a line beginning with
`#` as a comment and deletes **the whole line** — so `## Volume` does not lose
its markers, it disappears entirely. v2.12.0 shipped that way: the notes were
structurally correct in the file, five headings gone from the published body,
and the only visible sign was a wall of paragraphs. Fixing it afterwards means
`gh api -X PATCH repos/<owner>/<repo>/releases/<id> -F body=@notes.md`
(`gh release edit` has no `--notes-file`), and re-appending GitHub's generated
commit list by hand, since the PATCH replaces the whole body.

The controller's own version is resolved by `controller/version.py` (env `EM_CONTROLLER_VERSION` — baked into the image from the tag — then `git describe --match 'controller-v*'`, then `"dev"`). It's exposed at `/api/system/status` as `controller_version`, shown in the dashboard header, and reported to HA as the ESPHome project version.

`controller/docker-compose.yml` is the local dev/GPU build (`GPU=1` build arg swaps in onnxruntime-gpu); `controller/docker-compose.deploy.yml` is the user-facing compose that pulls the published image.

`device/tools/` contains standalone diagnostics (`capture_mics`, `bf_capture` + analysis scripts) for mapping the 9-channel mic array; they build inside the same compiler image.

## Architecture

### Device → Controller protocol

**The full wire contract is `docs/device-controller-interface.md`** (#347,
@dweng0) — every `/control` message both ways, the `/data` frame codes and
their direction-namespacing, config-push semantics, link auth, and the exact
capability list. It is written for someone building a device binary for a NEW
board against a specification rather than by reading `biscuit`'s source, and
it was more accurate about our own capability list than this file was. Keep
the summary below as a summary; put detail there.

Each device opens **three** WebSocket connections to the controller:

| Path | Direction | Purpose |
|------|-----------|---------|
| `/control` | bidirectional JSON | Registration, LEDs, mic_start/stop, button events, config push |
| `/data` | binary | Mic PCM frames in (0x01 header), speaker PCM frames out (0x02/0x03) |
| `/shell/{device_id}` | raw binary | Root shell proxy (demand-opened by device on `shell_open` command) |

Controller is discovered by the device via mDNS (`_emcontroller._tcp.local`).

### Device-link TLS + token auth

All three WS planes exist twice: plain on `SERVER_PORT` (8767) and TLS on `SERVER_TLS_PORT` (8770, `wss://`). `em_pki.py` generates a private CA + server cert on first start (persisted in `tls/` next to the SQLite DB; delete the dir to rotate — every device then needs a fresh credential push). The leaf's identity is the fixed DNS SAN `echomuse-controller` (`TLS_SERVER_NAME`, coupled with `tlsServerName` in `device/internal/client/tlscreds.go`) — never an IP, so the controller can move address freely. Certs are backdated 10y/valid 25y **and** the device clamps its verification clock to the firmware build time (`BuildUnix` ldflag): Echos boot with bogus clocks pre-NTP, and a device that can't connect can't fix its clock. Don't "normalise" either half of that.

Device behaviour (`tlscreds.go`): credentials live at `/data/local/etc/echomuse/{ca.pem,token}` (canonical path constant: `em_api.DEVICE_TLS_DIR`) and are **re-read on every dial**, so a push takes effect on the next reconnect, no restart. CA present + `tls_port` mDNS TXT property → dial wss; CA present but no TXT → plain with a warning (deliberate rollout fallback). The token rides as `X-EM-Token` on all three dials.

Controller enforcement (`em_linkauth.decide`, called by `_link_auth_ok`): presented-but-wrong token always rejects; stored-token-but-none-presented is allowed (the credential push itself rides the plain shell plane, and rejecting there would deadlock the rollout); a token presented for a device with NOTHING on record is **ignored, not rejected**. Rejecting it made deleting a device a one-way door, since delete takes the token with the row while the device keeps re-reading its credential file, and the refusal covered the shell plane the controller would have fixed it over. It also bought nothing: a connection presenting no token at all is already allowed, so an attacker just omits the header. `REQUIRE_DEVICE_TLS=1` flips the posture to TLS+token mandatory and is unaffected by that: a deleted device is still refused there and needs credentials pushed over USB. Flip it only when every device shows `wss (TLS)` in the dashboard (Status tab "Link" row; `linkTls` in `/api/devices`).

**Deleting a device must also close its control plane, and `_delete_device` does.** Link auth is decided ONCE, at register time, so removing the row does nothing to the socket a connected device is already on: it vanishes from the dashboard and carries on serving turns, holding its ESPHome port and wake-listening, and only comes back as pending when something else drops the link. The tell is `sqlite3.IntegrityError: FOREIGN KEY constraint failed` in `db.log_device` every time the orphan relays a log line — `device_logs` references `devices(device_id)` and the parent is gone — which is how this was found on the live EA controller, 2026-08-27, a device deleted five minutes earlier and still perfectly connected. The bounce goes **after** the row is deleted: the device redials in 5s, and closing first races the redial against the delete.

Credential delivery: the provisioning wizard installs credentials over adb pre-first-contact (`POST /api/provision/tls_credentials` mints the token + pending device row from the serial); already-fleet devices get the dashboard **Secure link** action (`POST /api/devices/{id}/secure_link` — shell-plane file push, then a connection bounce to redial over wss).

## Device config push

`config.ConfigMessage` JSON fields (camelCase) are sent from controller to device on connect and on per-device config change. Non-zero fields are applied; zero/nil fields are ignored (partial update). Changes take effect immediately — no restart required.

Configurable parameters: `vadThreshold`, `vadSpeechMs`, `vadSilenceMs`, `owwThreshold`, `owwModel`, `owwSpeexNs`, `adcDigitalGain`, `adcMicpga`, `micGainDb`, `startupVolume`, `beamAngle`, `beamformingEnabled`, `aecEnabled`, `aecDelayMs`, `aecTailMs`, `aecRefSource`, `agcEnabled`, `nsAsr`, `bargeInEnabled`, `bargeInThreshold`, `bleProxyEnabled`, `eqBands`, `eqLoudness`, `limiterEnabled`, `limiterThreshold`, `limiterRelease`, `bassGuardEnabled`, `bassGuardDb`, `ledScene`, `ledListenColor`, `ledThinkColor`, `meterAttack`, `meterDecay`, `meterFloor`, `meterGamma`, `meterRef`, `meterCurve`, `wakeArbitrationMs`, `duckDb`, `buttonSingleTapEvent`, `buttonMultiTapMs`, `owwOnDevice` and `saveUtterances` (the last two are controller-consumed for scoping purposes, though `owwOnDevice` IS acted on by the device; `saveUtterances`, `wakeArbitrationMs`, the two `button*` keys and the five output-chain keys — `limiter*` and `bassGuard*` — are ignored by it, because that processing all happens controller-side before the audio reaches the wire).

## Build and test quickref

```bash
git submodule update --init          # GoTinyAlsa fork — see device/CLAUDE.md
cd device && ./compile.sh            # needs the echomuse-compiler image
cd device && go test ./...
cd controller && python -m pytest tests/   # needs: pytest numpy scipy pyyaml
```

Both suites plus `go vet` run in CI on every push/PR
(`.github/workflows/ci.yml`). Controller tests deliberately cover the
pure-logic modules only — see `controller/CLAUDE.md` before adding one that
needs openwakeword or aiohttp.
