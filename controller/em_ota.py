"""
Firmware updates: one device at a time across the whole controller, each
verified by md5 before it can replace a slot, watched until the device comes
back on the new version, and rolled back by the device itself if it does not.

`queue` is the fleet-wide lock (see "OTA update system" in
controller/CLAUDE.md); the emOS update and the wake word asset install hold
it too, for the same reason.
"""

from __future__ import annotations

import asyncio
import logging
import time

import em_db as db
import em_events
import em_oww_assets
import em_payloads
import em_releases
import em_shell
import em_state

log = logging.getLogger("echomuse.api")

# Track in-progress OTA updates per device_id to enforce one-at-a-time.
in_progress: set[str] = set()

# Firmware updates are SERIALISED across the whole controller, not just per
# device. Three concurrent OTAs stalled the event loop for 11.1 seconds on
# 2026-09-02 — measured, in the `[loop] event loop stalled` warnings — and
# that loop is what sends speaker periods and LED frames, so a device mid
# response pays for a device being updated. The transfer is base64 over the
# shell plane and CPU-bound in this process. Serialising stops several
# uploads stacking. One upload still has to yield: a single Dot's firmware
# send stalled this loop 2.2s and 13.8s on 2026-10-16 (the encode, then the
# heredoc, whose send does not return to the loop while the socket keeps
# accepting data). em_shell.stream_file encodes and hashes off the loop and
# yields during the send.
#
# A device-level guard cannot do this: `in_progress` stops one device
# being updated twice, and says nothing about two devices being updated at
# once. So the lock is global and BOTH entry points go through it — the fleet
# deploy and a hand-clicked single update, which collide identically.
#
# A failure does NOT stop the queue. Wil's call, 2026-09-02: mark it, carry on,
# report at the end — one device that will not come back should not strand the
# rest of a fleet update behind it.
queue = asyncio.Lock()

# Longest one device may hold the OTA queue. A real update is a ~10MB transfer,
# a reboot and a 90s reconnect watch, so this is a deadlock cap rather than a
# performance budget: it exists because serialising made one wedged device able
# to block the whole fleet's updates until the controller restarted.
MAX_HOLD_S = 300.0

# Devices waiting on `queue`. Reported separately from
# `update_in_progress` so the dashboard says "queued" rather than claiming
# work that has not started — the same rule as everywhere else here: a control
# that cannot act must say so rather than appear to work.
queued: set[str] = set()

# Last OTA failure per device, surfaced as `update_error` in /api/devices so
# the dashboard (fleet deploy modal + per-device update log) can show *why* a
# tile stopped progressing instead of sitting at "updating…" forever. Set by
# _update_failed on every run_update/run_rollback failure path; cleared when
# a new update starts and on confirmed success. In-memory by design — a
# controller restart clears stale errors along with the update tasks
# themselves.
errors: dict[str, str] = {}

# Devices whose last update failed and whose supervisor log has not yet been
# collected. The fetch cannot happen at failure time — the device is gone,
# which is the whole problem — so it waits for the next connect.
supervisor_log_wanted: set[str] = set()


def extract_binary_version(binary: bytes) -> str | None:
    """
    Scan a compiled Go binary for its embedded EchoMuse version string.

    Two schemes, because compile.sh changed and this did not:

      v2.12.0-63-g99628d3   `git describe --tags --match 'v*'` — CURRENT
      20260614-1152-dev     date+suffix — used when the tree is DIRTY, and
                            the only scheme this function knew until now

    Matching only the second is why a clean-tree local build extracted
    nothing, fell back to a `local-<timestamp>` label, and then never matched
    the version the device reported on reboot — so a completely successful
    deploy was announced as "auto-rolled back". A message that says the
    opposite of what happened is worse than a plain failure, because the
    natural response is to distrust the feature.

    BARE `vX.Y.Z` IS DELIBERATELY NOT MATCHED, and that is the whole
    subtlety. A Go binary embeds its dependencies' module versions, so the
    shape is far from unique — measured on the v2.12.0-63-g99628d3 build:
    102 occurrences, 9 distinct, including v0.41.0 (x/sys), v1.5.3
    (gorilla/websocket), v1.0.3 (GoTinyAlsa) and v1.1.41 (miekg/dns). Our own
    version happened to appear first in file order on that build, which is
    luck and not a property to rely on: a dependency string landing earlier
    would silently extract the wrong version and label the deploy with it.

    The `-N-gSHA` suffix makes it unique — exactly one match on the same
    binary — so only the suffixed form is accepted. A build made exactly ON a
    tag has no suffix and extracts nothing, which is correct: that binary
    belongs on the release path, and returning None gets an honest
    `local-<timestamp>` label rather than a confident wrong one.

    Returns None when nothing matches; the caller labels it
    `local-YYYYMMDD-HHMM` and the reconnect check compares against the
    version the device had BEFORE the push rather than against this.
    """
    import re as _re
    for pattern in (
        # git describe, suffixed form only — see above
        rb'v\d+\.\d+\.\d+-\d+-g[0-9a-f]{7,40}',
        # dirty-tree builds, and every binary predating the compile.sh change
        rb'20\d{6}-\d{4}-[a-z][a-z0-9]*',
    ):
        match = _re.search(pattern, binary)
        if match:
            return match.group(0).decode("ascii")
    return None


async def _update_failed(device_id: str, reason: str) -> None:
    """
    Record and broadcast an OTA failure: device log line, in-memory
    update_error (read back via /api/devices), and a device_update_failed
    event for the dashboard WS. Every abort path in run_update/run_rollback
    must come through here — a log-only failure leaves the dashboard tile at
    "updating…" indefinitely.
    """
    errors[device_id] = reason
    await em_events.push_log(device_id, "error", "controller", reason)
    await em_events.push({
        "type":      "device_update_failed",
        "device_id": device_id,
        "error":     reason,
    })


async def run_update(device_id: str, release: dict,
                      binary_override: bytes | None = None) -> None:
    """
    Background task: A/B slot update.

    1. Fetch binary (GitHub or pre-uploaded).
    2. Detect active slot via readlink; migrate legacy layout if needed.
    3. Stream binary to inactive slot.
    4. Flip symlink atomically.
    5. Restart service and monitor reconnect.
    6. Detect auto-rollback (start_server.sh retry exhausted).
    """
    errors.pop(device_id, None)  # fresh attempt clears the last failure

    # Wait for any other device's update to finish before touching this one.
    # Queued state is visible while waiting (see `queued`), and the
    # binary is fetched INSIDE the lock so a queued device is holding nothing
    # but its place in line.
    queued.add(device_id)
    try:
        await queue.acquire()
    finally:
        queued.discard(device_id)

    in_progress.add(device_id)
    try:
        # Bounded, because serialising turned a device-local stall into a
        # fleet-wide one. Every `recv` in the transfer is wait_for-bounded but
        # `await ws.send(line)` in the base64 loop is not: a device that stops
        # reading applies backpressure and can hang there. Before the lock that
        # stalled one device; now it would hold the queue and NOTHING could be
        # updated until the controller restarted.
        #
        # Generous on purpose — a real update is a ~10MB transfer plus a reboot
        # and a 90s reconnect watch, so this is a deadlock cap, not a
        # performance budget. A device that trips it has failed anyway.
        await asyncio.wait_for(
            _run_update_locked(device_id, release, binary_override),
            timeout=MAX_HOLD_S,
        )
    except asyncio.TimeoutError:
        await _update_failed(
            device_id,
            f"Update abandoned after {MAX_HOLD_S:.0f}s so the queue "
            f"could continue — the device may still be mid-update",
        )
    finally:
        in_progress.discard(device_id)
        # Release LAST, so the next queued device does not start its transfer
        # while this one is still being cleaned up.
        queue.release()


async def _run_update_locked(device_id: str, release: dict,
                             binary_override: bytes | None = None) -> None:
    """
    The update itself, run with `queue` already held. Split from
    `run_update` so the whole of it can sit under one timeout — the queue is
    only as safe as its slowest member.
    """
    loop = asyncio.get_event_loop()
    version = release["version"]

    try:
        await em_events.push_log(device_id, "info", "controller",
                              f"OTA update starting → {version}")

        # Fetch binary
        if binary_override is not None:
            binary = binary_override
            await em_events.push_log(device_id, "info", "controller",
                                  f"Using uploaded binary ({len(binary):,} bytes)")
        else:
            binary = await em_releases.download(release["url"], release.get("version", ""))
            if binary is None:
                await _update_failed(device_id,
                                     "Failed to fetch binary from GitHub")
                return

        # Record current version as previous before anything changes
        row = await loop.run_in_executor(None, db.get_device, device_id)
        current_ver = row["firmware_ver"] if row else None
        await loop.run_in_executor(None, db.set_firmware_previous, device_id, current_ver)

        live = em_state.devices.get(device_id)
        if live is None:
            await _update_failed(device_id,
                                 "Device disconnected before update could start")
            return

        # Detect active slot and migrate legacy layout if needed — single shell
        # session to avoid the race condition of two sequential open/close cycles.
        detect_cmd = (
            "CURRENT=$(readlink /data/local/bin/server 2>/dev/null); "
            "if [ \"$CURRENT\" = \"server_a\" ] || [ \"$CURRENT\" = \"server_b\" ]; then "
            "  echo \"SLOT:$CURRENT\"; "
            "else "
            "  cp /data/local/bin/server /data/local/bin/server_a 2>&1 && "
            "  chmod 755 /data/local/bin/server_a && "
            "  ln -sf server_a /data/local/bin/server && "
            "  echo \"SLOT:server_a MIGRATED\" || echo \"MIGRATE_FAILED\"; "
            "fi"
        )
        detect_result = await em_shell.run(live, detect_cmd, timeout=60.0)
        log.info(f"[api] Slot detect result for {device_id}: {detect_result!r}")

        if "MIGRATE_FAILED" in detect_result:
            await _update_failed(device_id,
                                 "A/B migration failed — aborting update")
            return

        active_slot = None
        for line in detect_result.splitlines():
            if "SLOT:" in line:
                candidate = line.split("SLOT:")[-1].strip().split()[0]
                if candidate in ("server_a", "server_b"):
                    active_slot = candidate
                    break

        if active_slot is None:
            await _update_failed(device_id,
                                 f"Could not determine active slot — output: {detect_result!r}")
            return

        if "MIGRATED" in detect_result:
            await em_events.push_log(device_id, "info", "controller",
                                  "A/B migration complete — active slot: server_a")

        # Sync the startup script while we're here — OTA is the only update
        # path existing devices have for it (see em_payloads.sync_start_script).
        #
        # NOT gated on the platform, deliberately: emOS runs this same script.
        # Its init supervises `/system/bin/sh /data/local/bin/start_server.sh`
        # (emos/init/init.c), because the script owns the A/B slot symlink and
        # the fast-exit backoff, which both bases need. Gating it here by
        # symmetry with the debloat below would strand emOS devices on whatever
        # script they were provisioned with.
        await em_payloads.sync_start_script(live, device_id)
        # Payload drift is not limited to the start script — the debloat
        # halves had no update path at all until 2026-07-30.
        #
        # Gated, because the debloat is Android-only: a pm-hide list and a
        # Magisk service.d script, and emOS has neither a package manager nor
        # Magisk. This was the THIRD call site of em_payloads.sync_debloat and the only
        # one that did not ask — em_reconcile.on_connect and _post_debloat both
        # check. Found on EFF 2026-09-07 at its first OTA onto emOS: the
        # transfer targeted /sbin/.core/img/.core/service.d/ on a device with
        # no Magisk daemon to have created it.
        #
        # It cost only a wasted shell round trip because the destination
        # directory probe caught it. Without that probe it is the 240s stall
        # measured on this same device on 2026-09-04 — TRANSFER_OK never
        # arrives and the transfer holds the shell lock for its full timeout,
        # twice. The gate belongs here anyway: the probe bounds the damage of
        # a payload that should never have been sent.
        if live.android_userspace:
            await em_payloads.sync_debloat(live, device_id)

        inactive_slot = "server_b" if active_slot == "server_a" else "server_a"

        # Free space, checked BEFORE writing anything. The transfer needs room
        # for the new binary alongside its .part, and running /data out of
        # space mid-write is a bad way to find out. Read with parse_free_mb,
        # never an awk field index — busybox wraps a long filesystem name onto
        # its own line, so $4 is the percentage on these devices.
        need_mb  = (len(binary) * 2) // 1048576 + 8   # binary + .part + slack
        free_out = await em_shell.run(
            live, 'echo "FREE $(busybox df -m /data | busybox tail -1)"')
        free_mb = None
        for line in (free_out or "").splitlines():
            if line.startswith("FREE"):
                free_mb = em_oww_assets.parse_free_mb(line[5:])
                break
        if free_mb is not None and free_mb < need_mb:
            await _update_failed(
                device_id,
                f"Not enough space on /data: {free_mb}MB free, needs ~{need_mb}MB")
            return
        if free_mb is None:
            # Unknown reads as "carry on" — a df we cannot parse is not
            # evidence of a full disk, and refusing on it would block updates
            # on any device whose df we have not seen.
            log.warning(f"[api] Could not read free space on {device_id} "
                        f"from {free_out!r} — proceeding with update")

        await em_events.push_log(device_id, "info", "controller",
                              f"Deploying to slot {inactive_slot} (active: {active_slot})")

        # Stream binary to inactive slot. Verified by md5 before it is renamed
        # into place, so a corrupt transfer leaves the slot as it was and never
        # reaches the symlink flip below (#76).
        ok = await _stream_binary_to_slot(live, binary, inactive_slot)
        if not ok:
            # Name the stage. "failed or did not verify" covered everything
            # from a shell that never opened to a corrupt payload, and #121
            # was the former reported in the language of the latter.
            await _update_failed(device_id,
                                 f"Binary transfer to {inactive_slot} failed: "
                                 f"{ok} — {inactive_slot} left untouched")
            return

        # Brief pause so device can cleanly close the transfer shell before
        # we open a new one for the symlink flip.
        await asyncio.sleep(1.0)

        # Atomic symlink flip + service restart
        await em_events.push_log(device_id, "info", "controller",
                              f"Flipping symlink → {inactive_slot} and restarting")
        result = await em_shell.run(live,
            f"ln -sf {inactive_slot} /data/local/bin/server && "
            f"kill $PPID"
        )
        # Shell dies when the server process is killed — FLIP_OK will never arrive.
        # _monitor_reconnect below detects whether the restart succeeded.

        # Wait for device to come back
        confirmed = await _monitor_reconnect(device_id, version, previous_version=current_ver, timeout=90)

        if confirmed:
            errors.pop(device_id, None)
            await em_events.push_log(device_id, "info", "controller",
                                  f"✓ Update confirmed: {version}")
            await em_events.push({
                "type":      "device_updated",
                "device_id": device_id,
                "version":   version,
            })
        else:
            row     = await loop.run_in_executor(None, db.get_device, device_id)
            running = row["firmware_ver"] if row else "unknown"
            outcome = failed_update_outcome(device_id in em_state.devices,
                                            running, current_ver)

            if outcome == "not_back":
                errors[device_id] = (
                    "did not reconnect within 90s of the restart — "
                    f"last reported {running}"
                )
                supervisor_log_wanted.add(device_id)
                await em_events.push_log(device_id, "warn", "controller",
                    f"Update to {version}: the device has not reconnected "
                    f"since the restart (last reported {running})")
                await em_events.push({
                    "type":      "device_update_failed",
                    "device_id": device_id,
                    "error":     errors[device_id],
                    "running":   running,
                })
            elif outcome == "rolled_back":
                # Device came back on old version — auto-rollback by start_server.sh
                await loop.run_in_executor(
                    None, db.set_firmware_previous, device_id, None
                )
                errors[device_id] = (
                    f"auto-rolled back to {running} — new binary failed to start"
                )
                supervisor_log_wanted.add(device_id)
                await em_events.push_log(device_id, "warn", "controller",
                    f"Device auto-rolled back to {running} "
                    f"— new binary failed {3} start attempts")
                await em_events.push({
                    "type":      "device_auto_rolled_back",
                    "device_id": device_id,
                    "version":   running,
                })
            else:
                errors[device_id] = (
                    f"timed out — device running {running}"
                )
                supervisor_log_wanted.add(device_id)
                await em_events.push_log(device_id, "warn", "controller",
                    f"Update timed out — device running: {running}")
                await em_events.push({
                    "type":      "device_update_failed",
                    "device_id": device_id,
                    "error":     errors[device_id],
                    "running":   running,
                })

    except Exception as e:
        log.exception(f"[api] OTA update error for {device_id}: {e}")
        await _update_failed(device_id, f"OTA exception: {e}")


def failed_update_outcome(connected: bool, running: str | None,
                          previous: str | None) -> str:
    """
    What an update that was not confirmed within the watch actually did.

    "rolled_back" needs the device BACK and on its old version: that is
    start_server.sh giving up on the new binary after three fast exits. The
    stored version cannot say it alone, because it only changes when a device
    registers, so a device that never came back still holds the old one; read
    without `connected`, every Echo that died on its update was reported as
    "auto-rolled back", which reads as healthy (found driving an OTA at a fake
    Echo that did not redial, 2026-10-15). "not_back" is that case, and
    "timeout" is a device back on something else.
    """
    if not connected:
        return "not_back"
    if running == previous:
        return "rolled_back"
    return "timeout"


async def run_rollback(device_id: str, target_version: str) -> None:
    """
    Background task: flip to inactive A/B slot.

    No binary transfer needed — the old binary is already in the inactive slot.
    """
    in_progress.add(device_id)
    errors.pop(device_id, None)  # fresh attempt clears the last failure
    try:
        await em_events.push_log(device_id, "info", "controller",
                              f"Rolling back to {target_version}")

        live = em_state.devices.get(device_id)
        if live is None:
            await _update_failed(device_id,
                                 "Device disconnected before rollback")
            return

        active_slot = None
        detect_result = await em_shell.run(live,
            "CURRENT=$(readlink /data/local/bin/server 2>/dev/null); "
            "if [ \"$CURRENT\" = \"server_a\" ] || [ \"$CURRENT\" = \"server_b\" ]; then "
            "  echo \"SLOT:$CURRENT\"; "
            "else echo \"SLOT_UNKNOWN\"; fi"
        )
        for line in detect_result.splitlines():
            if "SLOT:" in line:
                candidate = line.split("SLOT:")[-1].strip().split()[0]
                if candidate in ("server_a", "server_b"):
                    active_slot = candidate
                    break

        if active_slot is None:
            await _update_failed(device_id,
                                 "Cannot determine active slot — is A/B set up?")
            return

        inactive_slot = "server_b" if active_slot == "server_a" else "server_a"
        await em_events.push_log(device_id, "info", "controller",
                              f"Flipping {active_slot} → {inactive_slot}")

        result = await em_shell.run(live,
            f"ln -sf {inactive_slot} /data/local/bin/server && "
            f"kill $PPID"
        )
        # Shell dies when the server process is killed — ROLLBACK_OK will never arrive.

        loop = asyncio.get_event_loop()
        row_pre = await loop.run_in_executor(None, db.get_device, device_id)
        current_fw = row_pre["firmware_ver"] if row_pre else None
        confirmed = await _monitor_reconnect(
            device_id, target_version,
            previous_version=current_fw,
            timeout=90,
        )

        if confirmed:
            errors.pop(device_id, None)
            await loop.run_in_executor(
                None, db.set_firmware_previous, device_id, None
            )
            await em_events.push_log(device_id, "info", "controller",
                                  f"✓ Rollback confirmed: {target_version}")
            await em_events.push({
                "type":      "device_rolled_back",
                "device_id": device_id,
                "version":   target_version,
            })
        else:
            await _update_failed(device_id,
                                 "Rollback did not reconnect within 90s")

    except Exception as e:
        log.exception(f"[api] Rollback error for {device_id}: {e}")
        await _update_failed(device_id, f"Rollback exception: {e}")
    finally:
        in_progress.discard(device_id)


async def _monitor_reconnect(
    device_id: str,
    expected_version: str,
    previous_version: str | None = None,
    timeout: int = 90,
) -> bool:
    """
    Poll until the device reconnects on a new version, or timeout elapses.

    Accepts success if the device reports expected_version exactly (GitHub
    releases where the tag matches the binary's embedded version), OR any
    version that differs from previous_version (local uploads where the
    binary reports its own version string, not the controller's local-YYYYMMDD
    tracking string).
    """
    loop     = asyncio.get_event_loop()
    deadline = time.monotonic() + timeout
    await asyncio.sleep(8)  # give device time to stop and restart

    while time.monotonic() < deadline:
        if device_id in em_state.devices:
            row = await loop.run_in_executor(None, db.get_device, device_id)
            if row:
                running = row["firmware_ver"]
                if running == expected_version:
                    return True
                if previous_version is not None and running != previous_version:
                    return True
        await asyncio.sleep(2)

    return False


async def _stream_binary_to_slot(live, binary: bytes, slot: str) -> "em_shell.TransferResult":
    """
    Transfer a firmware binary to /data/local/bin/{slot}.

    require_verify=True: firmware is the one payload where an unverifiable
    transfer must fail rather than proceed. A corrupt binary and a genuinely
    broken one produce the same observable — three fast exits and a rollback —
    so shipping one to the slot we are about to boot costs a reboot and a
    rollback to learn nothing (#76).
    """
    return await em_shell.stream_file(live, binary, f"/data/local/bin/{slot}",
                                      require_verify=True)
