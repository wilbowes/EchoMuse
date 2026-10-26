"""
Updating emOS in place on a device: the controller's side of #573.

em_emos_update holds the checks and the sequence against an `io` the tests
can stand in for; this is the real `io` (_EmosIO, over the shell plane), the
queue it waits in (em_ota.queue, like firmware), the state the Updates tab
shows, the check run on every emOS connect that confirms or reports the last
update, and fetching the release's init and payload for it and for the
provisioning wizard. See "emOS update" in controller/CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import em_db as db
import em_dbwriter
import em_emos_build
import em_emos_update
import em_events
import em_http
import em_ota
import em_releases
import em_shell
import em_state

log = logging.getLogger("echomuse.api")

# emOS updates (#573, em_emos_update). Their own sets rather than the firmware
# ones above: the dashboard shows the two side by side, and a shared flag
# would put "updating…" on the firmware panel while emOS is being flashed.
# Both endpoints refuse while EITHER is running on a device.
in_progress: set[str] = set()


queued: set[str] = set()


errors: dict[str, str] = {}


# The step an update is on, for the panel. Cleared when it ends.
stage: dict[str, str] = {}


# An emOS update holds the queue through the reboot and, when the new image
# fails, through init's whole trial and rollback.
MAX_HOLD_S = em_emos_update.WATCH_S + 600.0


def busy(device_id: str) -> bool:
    return (device_id in in_progress
            or device_id in queued)


async def _update_failed(device_id: str, reason: str) -> None:
    """Every abort path comes through here, as _update_failed's do."""
    errors[device_id] = reason
    # The controller's own log as well as the device's: on C95's first run
    # the only trace here was the file transfer, with no outcome either way.
    log.warning(f"[api] [{device_id}] emOS update failed: {reason}")
    await em_events.push_log(device_id, "error", "controller",
                          f"emOS update: {reason}")
    await em_events.push({"type": "device_emos_update_failed",
                       "device_id": device_id, "error": reason})


async def run_update(device_id: str, override: tuple | None = None) -> None:
    """Background task: queue behind `em_ota.queue`, then update under a cap."""
    errors.pop(device_id, None)
    queued.add(device_id)
    try:
        await em_ota.queue.acquire()
    finally:
        queued.discard(device_id)

    in_progress.add(device_id)
    try:
        await asyncio.wait_for(_run_update_locked(device_id, override),
                               timeout=MAX_HOLD_S)
    except asyncio.TimeoutError:
        await _update_failed(
            device_id,
            f"abandoned after {MAX_HOLD_S:.0f}s so the queue could "
            f"continue — the device may still be mid-update")
    except Exception as e:
        log.exception(f"[api] emOS update error for {device_id}: {e}")
        await _update_failed(device_id, f"exception: {e}")
    finally:
        in_progress.discard(device_id)
        stage.pop(device_id, None)
        em_ota.queue.release()


async def _emos_sh(live, cmd: str, timeout: float = 60.0) -> str:
    """
    Run `cmd` on the device and return what it printed up to em_emos_update.OK.

    Not em_shell.run: that gives up after 5s of SILENCE, and a boot partition
    write is silent for longer than that. Returning early would also close the
    session under a dd that is still running. This waits the whole timeout,
    and a reply without OK is "did not finish" to every parser.
    """
    device_id = live.device_id
    buf = ""
    try:
        ws = await em_shell.get_ws(live)
        await ws.send(cmd + "\n")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and em_emos_update.OK not in buf:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            buf += (msg.decode("utf-8", errors="replace")
                    if isinstance(msg, bytes) else msg)
    except Exception as e:
        log.warning(f"[api] [{device_id}] emOS shell step did not finish: {e}")
    finally:
        await em_shell.release_ws(device_id, live)
    await asyncio.sleep(1.0)        # let the session close before the next
    return buf


# `step` takes a `log` argument, which hides the module's logger inside it.
_emos_log = log


class _EmosIO:
    """What em_emos_update.run_update needs from the controller."""

    def __init__(self, device_id: str, live, override: tuple | None):
        self.device_id, self.live, self.override = device_id, live, override
        # As its firmware reported it: the packer needs it for a board whose
        # image carries a recipe (em_emos_build.CMDLINE_RECIPES).
        self.board = em_state.boards.get(device_id)

    async def sh(self, cmd: str, timeout: float) -> str:
        return await _emos_sh(self.live, cmd, timeout)

    async def send(self, data: bytes, dest: str) -> str:
        sent = await em_shell.stream_file(self.live, data, dest, mode="600",
                                            require_verify=True)
        await asyncio.sleep(1.0)
        return "" if sent else str(sent)

    async def payload(self, arch: str) -> tuple:
        if self.override is not None:
            init_bin, sbin, version, err = select_payload(
                self.override[0], arch, self.override[1], self.board)
        else:
            init_bin, sbin, version, err = await fetch_payload(arch, self.board)
        if err is not None:
            try:
                reason = json.loads(err.text)["error"]
            except Exception:
                reason = "the emOS release could not be fetched"
            raise em_emos_update.Refused(reason + em_emos_update.UNCHANGED)
        return init_bin, sbin, version

    async def offload(self, fn, *args):
        # Gzipping a ramdisk and hashing two images blocks the loop for long
        # enough to matter while other devices stream audio through it.
        return await asyncio.get_event_loop().run_in_executor(None, fn, *args)

    async def step(self, msg: str, log: bool = True) -> None:
        stage[self.device_id] = msg
        if log:
            _emos_log.info(f"[api] [{self.device_id}] emOS update: {msg}")
            await em_events.push_log(self.device_id, "info", "controller",
                                  f"emOS update: {msg}")

    async def record(self, version, build) -> None:
        em_dbwriter.submit(db.set_device_emos, self.device_id, version, build)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def now(self) -> float:
        return time.monotonic()

    def relink(self) -> str:
        current = em_state.devices.get(self.device_id)
        if current is self.live:
            return "same"
        if current is None:
            return "gone"
        self.live = current
        return "new"


async def _run_update_locked(device_id: str, override: tuple | None) -> None:
    live = em_state.devices.get(device_id)
    if live is None:
        return await _update_failed(
            device_id, "the device disconnected before the update started")
    try:
        version = await em_emos_update.run_update(
            _EmosIO(device_id, live, override))
    except em_emos_update.Refused as e:
        return await _update_failed(device_id, str(e))
    errors.pop(device_id, None)
    log.info(f"[api] [{device_id}] emOS update confirmed: {version}")
    await em_events.push_log(device_id, "info", "controller",
                          f"✓ emOS update confirmed: {version}")
    await em_events.push({"type": "device_emos_updated",
                       "device_id": device_id, "version": version})


async def status_on_connect(live, device_id: str) -> None:
    """Record which emOS a connected device runs, and settle a trial mark the
    update task is no longer around to settle (em_emos_update.settle_on_connect)."""
    if busy(device_id):
        return
    sh = lambda cmd, timeout: _emos_sh(live, cmd, timeout)
    st, verdict = await em_emos_update.settle_on_connect(sh)
    if not st and em_state.devices.get(device_id) is live:
        await asyncio.sleep(em_shell.CONNECT_RETRY_S)      # shell plane not up yet
        if em_state.devices.get(device_id) is live and not busy(device_id):
            st, verdict = await em_emos_update.settle_on_connect(sh)
    if not st:
        return
    em_dbwriter.submit(db.set_device_emos, device_id, st["version"], st["build"])
    if verdict == "confirm":
        await em_events.push_log(device_id, "info", "controller",
                              f"✓ emOS update confirmed: {st['version']}")
    elif verdict == "incomplete":
        log.warning(f"[api] [{device_id}] an emOS update did not complete; "
                    f"running {st['version']}")
        await em_events.push_log(
            device_id, "warn", "controller",
            f"An emOS update did not complete or was rolled back: the Echo "
            f"is running {st['version']}. The image it left behind was removed.")
    elif verdict == "rolled_back":
        if st["rollback"]:
            msg = em_emos_update.rollback_text(st["rollback"], st["version"])
        else:
            msg = (f"emOS update to {st['mark'].get('version', '?')} was "
                   f"rolled back: the Echo is running {st['version']}")
        log.warning(f"[api] [{device_id}] {msg}")
        await em_events.push_log(device_id, "warn", "controller", msg)


# Which release asset carries the init for each kernel architecture. The init
# must match the device's KERNEL — see em_emos_build's ARCH_* constants — and
# these are the names emos-release.yml publishes.
INIT_ASSETS = {
    em_emos_build.ARCH_ARM64: "init",
    em_emos_build.ARCH_ARM: "init32",
}


# emOS's own userspace, installed into the image's /sbin. ONE build serves
# both kernels — these are ordinary processes, and a 64-bit kernel runs 32-bit
# binaries — so unlike the init there is nothing per-architecture here.
#
# busybox is here for the same reason the supplicant is: a FireOS 6 /system
# ships toybox and no busybox at all, so without ours there is no udhcpc and
# the image boots, associates, and never gets an address — plus no ntpd, no
# syslogd/klogd, and no awk for em-wifi to read a scan with.
#
# This tuple is an allowlist and a payload missing any member is REFUSED, so
# adding a name here strands every emOS release cut before it. Tag emOS first,
# then the controller.
SBIN_ASSETS = ("wpa_supplicant", "wpa_cli", "em-wifi", "busybox")


# The one member that rides in BOTH images. em-wifi is a shell script init
# never execs, and it resolves wpa_cli and wpa_supplicant the same
# /sbin-then-/system/bin way init does — so adding it to a FireOS 5 image
# changes nothing about which supplicant runs, which is the constraint the
# rest of this payload is held to.
#
# Without it a FireOS 5 device has no console way to set WiFi at all, and that
# is most of the fleet. Found on Office, 2026-09-21: emos-v0.8, /sbin/em-wifi
# absent, and correctly so under the old rule.
SBIN_BOTH_ARCHES = ("em-wifi",)


# One archive with a manifest of sha256s — see build_payload_bundle.
PAYLOAD_ASSET = "emos-payload.zip"


async def fetch_payload(arch: str, board: str | None = None) -> tuple:
    """Everything an emOS image needs for `arch`, from ONE release.

    Returns (init, sbin, version, error) — `sbin` being the /sbin tools the
    packer installs, and `error` a ready web.Response on failure and None on
    success, so every caller refuses identically. They are entry points to one
    question, and an answer that differed between them would be a bug nobody
    would look for.

    Comes from the release's bundle. A release predating it falls back to its
    loose `init`, which is enough for FireOS 5 — so today's fleet keeps
    provisioning with no new tag. FireOS 6 needs the bundle and says so.
    """
    release = await em_releases.fetch_emos()
    if release is None:
        return None, {}, "", em_http.error(
            "no_emos_release",
            "No published emOS release with an 'init' asset was found. Build "
            "one from emos/ with build.sh and select it by hand, or cut an "
            "emos-v* tag.", 404)

    version = release["version"]
    bundle_asset = release.get("assets", {}).get(PAYLOAD_ASSET)

    if bundle_asset is None:
        # Older release: FireOS 5 is served by the loose init, FireOS 6 cannot be.
        if arch != em_emos_build.ARCH_ARM64:
            return None, {}, version, em_http.error(
                "no_payload_bundle",
                f"emOS release {version} predates the payload bundle, so it "
                f"carries no {arch} init and none of emOS's WiFi tools — a "
                f"FireOS 6 image needs both. Cut a newer emos-v* tag.", 404)
        init, version, err = await _fetch_one_init(release, arch)
        return (None, {}, version, err) if err else (init, {}, version, None)

    raw = await em_releases.download(bundle_asset["url"],
                              f"{version}-{PAYLOAD_ASSET}")
    if raw is None:
        return None, {}, version, em_http.error(
            "fetch_failed",
            f"Could not download {PAYLOAD_ASSET} from GitHub", 502)

    # Checked against the digests the release recorded. Loud, because a bad
    # release is bad for everyone and the next move is a partition write.
    try:
        payload = await asyncio.get_event_loop().run_in_executor(
            None, em_emos_build.read_payload_bundle, raw)
    except em_emos_build.BuildError as e:
        log.error(f"[api] emOS release {version} carries an unusable payload "
                  f"bundle: {e}")
        return None, {}, version, em_http.error(
            "bad_release_asset",
            f"The payload in emOS release {version} is not usable: {e}", 502)

    return select_payload(payload["files"], arch, version, board)


def select_payload(files: dict, arch: str, version: str,
                   board: str | None = None) -> tuple:
    """Pick the init and /sbin tools for `arch` out of a bundle's files.

    Split from fetch_payload so an uploaded bundle (the emOS update's
    dev path) is selected from by the same rules as a release.
    """
    init_name = INIT_ASSETS.get(arch,
                                     INIT_ASSETS[em_emos_build.ARCH_ARM64])
    init = files.get(init_name)
    if init is None:
        return None, {}, version, em_http.error(
            "no_init_for_arch",
            f"emOS release {version} carries no '{init_name}', so there is no "
            f"init for this device's {arch} kernel. Cut a newer emos-v* tag, or "
            f"build one from emos/ with build.sh and select it by hand.", 404)

    # The manifest proves the bytes arrived intact, not that they are the right
    # kind of binary.
    problems = em_emos_build.init_binary_problems(init, arch)
    if problems:
        log.error(f"[api] emOS release {version} carries an unusable "
                  f"{init_name}: {'; '.join(problems)}")
        return None, {}, version, em_http.error(
            "bad_release_asset",
            f"The {init_name} in emOS release {version} is not usable: "
            f"{'; '.join(problems)}", 502)

    # The BINARIES are FireOS 6 only: a 32-bit kernel, or a board known to run
    # FireOS 6 on an arm64 one (em_emos_build.FIREOS6_BOARDS). init prefers
    # /sbin/wpa_supplicant the moment one exists, so including those in a
    # FireOS 5 image would move the whole fleet off Amazon's working
    # supplicant as a side effect. Same for wpa_cli, which init's reassociate
    # nudge now prefers.
    #
    # em-wifi is exempt and rides in both: it is a script nothing execs, so it
    # cannot change which supplicant init starts. Not fatal when missing — a
    # release predating it still builds a FireOS 5 image, exactly as before.
    sbin = {n: files[n] for n in SBIN_BOTH_ARCHES if n in files}
    if (arch == em_emos_build.ARCH_ARM
            or board in em_emos_build.FIREOS6_BOARDS):
        missing = [n for n in SBIN_ASSETS if n not in files]
        if missing:
            # Fatal: Amazon's supplicant cannot run under emOS, so the image
            # would have no WiFi and no way to report it but a cable.
            return None, {}, version, em_http.error(
                "no_wifi_tools_for_arch",
                f"emOS release {version} carries no {', '.join(missing)}. A "
                f"FireOS 6 image needs emOS's own userspace — Amazon's "
                f"supplicant cannot run under emOS and its /system has no "
                f"busybox — so there is nothing to build a working image "
                f"from. Cut a newer emos-v* tag.", 404)
        sbin.update({n: files[n] for n in SBIN_ASSETS})

    return init, sbin, version, None


async def fetch_init(arch: str) -> tuple:
    """Just the init, for the download endpoint: (binary, version, error)."""
    release = await em_releases.fetch_emos()
    if release is None:
        return None, "", em_http.error(
            "no_emos_release",
            "No published emOS release with an 'init' asset was found. Build "
            "one from emos/ with build.sh and select it by hand, or cut an "
            "emos-v* tag.", 404)
    return await _fetch_one_init(release, arch)


async def _fetch_one_init(release: dict, arch: str) -> tuple:
    """The init for `arch` out of an already-resolved release.

    Validation happens HERE, against the architecture that was asked for, so a
    release built wrong is refused at the point of download rather than at the
    point of boot — and every route to an init goes through this one function.
    """
    name = INIT_ASSETS.get(arch, INIT_ASSETS[em_emos_build.ARCH_ARM64])
    asset = release.get("assets", {}).get(name)
    if asset is None:
        # A release predating the second init, asked for the 32-bit one. Said
        # plainly rather than falling back to the 64-bit asset, which would
        # build an image that takes the flash and then produces no output at
        # all — the failure this whole path exists to prevent.
        return None, release["version"], em_http.error(
            "no_init_for_arch",
            f"emOS release {release['version']} carries no '{name}' asset, so "
            f"there is no init for this device's {arch} kernel. Cut a newer "
            f"emos-v* tag, or build one from emos/ with build.sh and select it "
            f"by hand.", 404)

    binary = await em_releases.download(asset["url"], f"{release['version']}-{name}")
    if binary is None:
        return None, release["version"], em_http.error(
            "fetch_failed", "Could not download the emOS init from GitHub", 502)

    problems = em_emos_build.init_binary_problems(binary, arch)
    if problems:
        # A release that is wrong is worth saying so about loudly: it is wrong
        # for everyone, not just this download.
        log.error(f"[api] emOS release {release['version']} carries an unusable "
                  f"{name}: {'; '.join(problems)}")
        return None, release["version"], em_http.error(
            "bad_release_asset",
            f"The {name} in emOS release {release['version']} is not usable: "
            f"{'; '.join(problems)}", 502)

    return binary, release["version"], None
