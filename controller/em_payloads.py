"""
Files the controller keeps in step on every device, pushed over the shell
plane: the start script, the debloat script and hide list, and the
controller address list (controller.json, #166).

Each sync compares the device's copy by md5 and pushes only on a difference,
and each tells a missing file from a device that did not answer
(em_shell.OK_MARK) before deciding anything. They run on connect
(em_api.reconcile_on_connect), during an OTA, and from the Maintenance
button; see "OTA update system" in controller/CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath
import re
from pathlib import Path

import em_db as db
import em_endpoints
import em_events
import em_shell
import em_state

log = logging.getLogger("echomuse.api")

# Device payloads — files the controller distributes to devices (provisioning
# wizard today; script/component OTA tomorrow). One canonical copy on disk,
# read per-request so edits ship without a restart. device/scripts/
# start_server.sh is a symlink into this directory.
PAYLOADS_DIR = Path(__file__).parent / "device_payloads"


async def sync_start_script(live, device_id: str) -> None:
    """
    OTA-time payload sync: heal /data/local/bin/start_server.sh drift.

    The startup script is installed at provisioning and — unlike the server
    binary — had no other update path, so fleet drift accumulates (found
    2026-07-11: Lounge was a script revision behind Office). Every OTA now
    compares the device's script against the canonical payload
    (controller/device_payloads/) and pushes it when they differ.

    Replacement is rename-based on purpose: the running script's shell keeps
    reading the OLD inode, so the update only takes effect at the next
    device reboot — safe to do while the script sits in its `wait` loop.
    Best-effort: a sync failure logs but never blocks the firmware update.
    """
    path = "/data/local/bin/start_server.sh"
    try:
        script = (PAYLOADS_DIR / "start_server.sh").read_bytes()
    except OSError as e:
        log.error(f"[api] start_server.sh payload unreadable — skipping sync: {e}")
        return
    want = hashlib.md5(script).hexdigest()

    # The trailing marker separates "the file differs" from "the device did not
    # answer". em_shell.run returns "" for both — it swallows the exception — and
    # an absent md5 was read as out-of-date, so a shell plane that is not up
    # yet produced a push attempt and a user-visible "out of date" event that
    # was not true. That was harmless while this only ran mid-OTA, with a shell
    # already proven; it is not, now that it also runs seconds after connect.
    out = await em_shell.run(live, f"busybox md5sum {path} 2>/dev/null; echo {em_shell.OK_MARK}")
    if em_shell.OK_MARK not in out:
        log.info(f"[api] [{device_id}] start_server.sh: no answer from the "
                 f"device — leaving it alone")
        return
    if want in out:
        return  # in sync — the common case
    await asyncio.sleep(1.0)  # let the md5 shell session close cleanly

    await em_events.push_log(device_id, "info", "controller",
                          "start_server.sh out of date — syncing canonical version")
    tmp = path + ".new"
    res = await em_shell.stream_file(live, script, tmp)
    if not res:
        await em_events.push_log(device_id, "warn", "controller",
                              f"start_server.sh sync failed: {res} — continuing OTA")
        return
    await asyncio.sleep(1.0)

    res = await em_shell.run(live,
        f'NEW=$(busybox md5sum {tmp} | busybox cut -d" " -f1); '
        f'if [ "$NEW" = "{want}" ]; then '
        f'mv {tmp} {path} && chmod 755 {path} && echo SCRIPT_SYNCED; '
        f'else rm -f {tmp}; echo SCRIPT_MD5_MISMATCH:$NEW; fi')
    if "SCRIPT_SYNCED" in res:
        await em_events.push_log(device_id, "info", "controller",
                              "start_server.sh synced — takes effect on next device reboot")
    else:
        await em_events.push_log(device_id, "warn", "controller",
                              f"start_server.sh sync failed ({res.strip() or 'no output'}) — continuing OTA")
    await asyncio.sleep(1.0)


def endpoints_file() -> bytes | None:
    cfg = db.get_global_device_config()
    return em_endpoints.file_bytes(cfg.get("controllerEndpoints") or [])


async def sync_endpoints(live, device_id: str) -> bool:
    """
    Make the device's controller.json match the fleet's controllerEndpoints.

    Takes effect at the device's next dial: firmware re-reads the file every
    attempt (#166), so nothing is bounced. An empty setting removes only a
    file this controller wrote (em_endpoints.MANAGED_KEY): #166 documented
    hand-writing one for devices that cannot use mDNS, and deleting it on
    upgrade would strand them. A set list replaces any file. Returns whether
    the device now matches.
    """
    path = em_endpoints.DEVICE_PATH
    want = endpoints_file()
    out = await em_shell.run(
        live, f"mkdir -p {posixpath.dirname(path)}; busybox md5sum {path} 2>/dev/null; "
              f"busybox grep -c '\"{em_endpoints.MANAGED_KEY}\"' {path} 2>/dev/null; "
              f"echo {em_shell.OK_MARK}")
    if em_shell.OK_MARK not in out:
        log.info(f"[api] [{device_id}] controller address: no answer from the "
                 f"device — leaving it alone")
        return False
    has_file = re.search(r"\b[0-9a-f]{32}\s", out) is not None
    managed = re.search(r"(?m)^[1-9]\d*$", out) is not None
    if want is None:
        if has_file and managed:
            await asyncio.sleep(1.0)
            res = await em_shell.run(live, f"rm -f {path}; [ -e {path} ] || echo {em_shell.OK_MARK}")
            if em_shell.OK_MARK in res:
                await em_events.push_log(device_id, "info", "controller",
                                      "Controller address list removed — mDNS only from the next reconnect")
            else:
                await em_events.push_log(device_id, "warn", "controller",
                                      "Controller address list not removed")
                return False
        return True
    if em_endpoints.md5(want) in out:
        return True
    if has_file and not managed:
        await em_events.push_log(device_id, "info", "controller",
                              "Replacing a hand-written controller.json with the fleet's address list")
    await asyncio.sleep(1.0)
    res = await em_shell.stream_file(live, want, path, mode="644")
    if res:
        await em_events.push_log(device_id, "info", "controller",
                              "Controller address list updated — used from the next reconnect")
        return True
    await em_events.push_log(device_id, "warn", "controller",
                          f"Controller address list not written: {res}")
    return False


# A push on save that fails is tried once more after this long, rather than
# leaving the device on its old list until it next reconnects.
ENDPOINTS_RETRY_S = 30.0


async def push_endpoints(live, device_id: str) -> None:
    if await sync_endpoints(live, device_id):
        return
    await asyncio.sleep(ENDPOINTS_RETRY_S)
    if em_state.devices.get(device_id) is live:
        await sync_endpoints(live, device_id)


# Magisk service.d location of the boot-time debloat script. Installed by the
# provisioning wizard; synced from here afterwards.
DEBLOAT_SCRIPT_PATH = "/sbin/.core/img/.core/service.d/echomuse-debloat.sh"


def debloat_packages() -> list[str]:
    """The pm-hide list from the canonical payload, comments stripped."""
    try:
        raw = (PAYLOADS_DIR / "debloat_packages.txt").read_text()
    except OSError as e:
        log.error(f"[api] debloat_packages.txt unreadable: {e}")
        return []
    return [ln.strip() for ln in raw.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


async def sync_debloat(live, device_id: str) -> None:
    """
    Heal debloat drift on a device that is already in the field.

    The debloat has two halves and neither had an update path. The boot script
    was installed once by the provisioning wizard, and the pm-hide list was
    applied once at the same time — so a device provisioned before a list grew
    never receives the addition. Found 2026-07-30 when round 2 added
    com.amazon.whad and every existing device needed a manual push.

    Both halves are reconciled here, idempotently, so this is safe to run as
    often as you like:

      * the script by md5 against the canonical payload, replaced by rename so
        the running shell keeps reading the old inode (same reasoning as
        sync_start_script — it takes effect on the next device reboot);
      * the hide list by asking the device which of those packages are still
        VISIBLE and hiding only those. One `pm list packages` costs about a
        second; `pm hide` is slow enough per call that hiding all 32
        unconditionally would add half a minute for nothing.

    Best-effort throughout: a failure logs and returns, and never blocks the
    firmware update this normally rides along with.

    Note what hiding does NOT do: com.amazon.whad is PERSISTENT, so hiding it
    leaves the running instance alive until the next reboot (am force-stop is a
    no-op on it — measured). That matches the rest of the debloat's semantics
    rather than being a shortcoming of this function, and it is why the log
    line says "next reboot".
    """
    # ── half 1: the boot script ──────────────────────────────────────────────
    try:
        script = (PAYLOADS_DIR / "echomuse-debloat.sh").read_bytes()
    except OSError as e:
        log.error(f"[api] echomuse-debloat.sh payload unreadable — skipping sync: {e}")
        script = None

    if script is not None:
        want = hashlib.md5(script).hexdigest()
        # The marker is what makes an empty result readable — see
        # sync_start_script. It gates BOTH halves below: a device that did not
        # answer the md5 will not answer `pm` either, and half 2 would
        # otherwise report a hide-list sync failure that is really a shell
        # that was never up.
        out = await em_shell.run(
            live, f"busybox md5sum {DEBLOAT_SCRIPT_PATH} 2>/dev/null; echo {em_shell.OK_MARK}")
        if em_shell.OK_MARK not in out:
            log.info(f"[api] [{device_id}] debloat: no answer from the device "
                     f"— leaving it alone")
            return
        if want not in out:
            # An empty md5 also lands here — a device provisioned before the
            # script existed has no file at all, and installing it is right.
            await asyncio.sleep(1.0)
            await em_events.push_log(device_id, "info", "controller",
                                  "debloat script out of date — syncing canonical version")
            tmp = DEBLOAT_SCRIPT_PATH + ".new"
            pushed = await em_shell.stream_file(live, script, tmp)
            if pushed:
                await asyncio.sleep(1.0)
                res = await em_shell.run(live,
                    f'NEW=$(busybox md5sum {tmp} | busybox cut -d" " -f1); '
                    f'if [ "$NEW" = "{want}" ]; then '
                    f'mv {tmp} {DEBLOAT_SCRIPT_PATH} && chmod 755 {DEBLOAT_SCRIPT_PATH} '
                    f'&& echo DEBLOAT_SYNCED; '
                    f'else rm -f {tmp}; echo DEBLOAT_MD5_MISMATCH:$NEW; fi')
                await em_events.push_log(
                    device_id,
                    "info" if "DEBLOAT_SYNCED" in res else "warn", "controller",
                    "debloat script synced — daemon stops take effect on next device reboot"
                    if "DEBLOAT_SYNCED" in res else
                    f"debloat script sync failed ({res.strip() or 'no output'})")
            else:
                await em_events.push_log(device_id, "warn", "controller",
                                      f"debloat script sync failed: {pushed}")
            await asyncio.sleep(1.0)

    # ── half 2: the pm-hide list ─────────────────────────────────────────────
    pkgs = debloat_packages()
    if not pkgs:
        return
    # Built as a file rather than a long inline list: 30-odd package names is
    # over a kilobyte of command line, and a shell command that is *usually*
    # short enough is the kind of thing that breaks on the day someone adds the
    # thirty-third package.
    listing = ("\n".join(pkgs) + "\n").encode()
    remote_list = "/data/local/tmp/em_debloat_pkgs.txt"
    pushed = await em_shell.stream_file(live, listing, remote_list, mode="644")
    if not pushed:
        await em_events.push_log(device_id, "warn", "controller",
                              f"debloat hide-list sync failed: {pushed}")
        return
    await asyncio.sleep(1.0)

    # Two details here were learned by getting them wrong (2026-07-30).
    #
    # The list is iterated with `for` over a variable, NOT `while read < file`:
    # `pm` is a wrapper that starts app_process, and a command inside a read
    # loop can consume the loop's own stdin, silently ending it early. `pm hide`
    # also gets </dev/null for the same reason.
    #
    # And the end state is VERIFIED by re-listing rather than trusting the
    # return code of each hide, so a partial failure cannot read as success.
    #
    # The match is `grep -qx`, ANCHORED to the whole line, and that is the
    # important part. A shell `case "$VIS" in *"package:$p"*)` looks equivalent
    # and is not: `package:com.amazon.tcomm` is also a substring of
    # `package:com.amazon.tcomm.client`, so three packages appeared un-hidden
    # because a *different*, longer-named package was visible. That produced a
    # confident warning about a FireOS limitation that did not exist — `pm hide`
    # had worked, and dumpsys said hidden=true throughout.
    res = await em_shell.run(live,
        f'PKGS=$(cat {remote_list}); VIS=$(pm list packages); N=0; '
        f'for p in $PKGS; do '
        f'if echo "$VIS" | busybox grep -qx "package:$p"; then '
        f'pm hide "$p" >/dev/null 2>&1 </dev/null && N=$((N+1)); fi; '
        f'done; '
        f'VIS2=$(pm list packages); LEFT=""; '
        f'for p in $PKGS; do '
        f'if echo "$VIS2" | busybox grep -qx "package:$p"; then LEFT="$LEFT $p"; fi; '
        f'done; '
        f'rm -f {remote_list}; '
        f'echo HIDDEN_APPLIED:$N; echo STILL_VISIBLE:$LEFT', timeout=180.0)

    applied = 0
    for tok in res.split():
        if tok.startswith("HIDDEN_APPLIED:"):
            try:
                applied = int(tok.split(":", 1)[1])
            except ValueError:
                pass
    still = ""
    for line in res.splitlines():
        if line.strip().startswith("STILL_VISIBLE:"):
            still = line.split(":", 1)[1].strip()
    if applied:
        await em_events.push_log(device_id, "info", "controller",
                              f"debloat: hid {applied} newly-listed package(s) — "
                              f"persistent ones stop at the next device reboot")
    if still:
        await em_events.push_log(device_id, "warn", "controller",
                              f"debloat: {len(still.split())} package(s) could not be "
                              f"hidden and are still active: {still}")
    await asyncio.sleep(1.0)
