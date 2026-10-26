"""
The provisioning wizard's routes: the files it installs on an Echo (start
script, debloat pair, Magisk DB, firmware, wake word assets, the emOS init),
building an emOS image from the device's own boot image, and the diagnostics
it collects when a step fails. Link credentials and the controller address
list are with the link routes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3 as _sqlite3
import tempfile
import time
from pathlib import Path

from aiohttp import web

import em_auth as auth
import em_chunked
import em_db as db
import em_emos_build
import em_emos_ota
import em_http
import em_oww_assets
import em_payloads
import em_releases
import em_support
from version import VERSION as CONTROLLER_VERSION

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    app.router.add_get("/api/provision/start_script", _get_provision_start_script)
    app.router.add_get("/api/provision/debloat_script",   _get_provision_debloat_script)
    app.router.add_get("/api/provision/debloat_packages", _get_provision_debloat_packages)
    app.router.add_get("/api/provision/magisk_db",    _get_provision_magisk_db)
    app.router.add_get("/api/provision/latest_binary", _get_provision_latest_binary)
    app.router.add_get("/api/provision/oww_assets",    _get_provision_oww_manifest)
    app.router.add_get("/api/provision/oww_asset/{name}", _get_provision_oww_asset)
    app.router.add_post("/api/provision/diagnostics",     _post_provision_diagnostics)
    app.router.add_get("/api/provision/emos_init",     _get_provision_emos_init)
    app.router.add_post("/api/provision/emos_image",   _post_provision_emos_image)
    app.router.add_post("/api/provision/upload_chunk", _post_provision_upload_chunk)


def _read_payload(name: str) -> str:
    path = em_payloads.PAYLOADS_DIR / name
    if not path.is_file():
        raise web.HTTPInternalServerError(
            text=f"Payload {name} missing from {em_payloads.PAYLOADS_DIR} — broken install/image"
        )
    return path.read_text()


@auth.require_admin
async def _get_provision_start_script(request: web.Request) -> web.Response:
    """GET /api/provision/start_script — serves the EchoMuse startup script."""
    return web.Response(
        text=_read_payload("start_server.sh"),
        content_type='text/plain',
        headers={'Content-Disposition': 'attachment; filename="start_server.sh"'},
    )


@auth.require_admin
async def _get_provision_debloat_script(request: web.Request) -> web.Response:
    """GET /api/provision/debloat_script — the Magisk service.d boot script
    that re-stops init-launched daemons each boot (Debloat wizard step)."""
    return web.Response(
        text=_read_payload("echomuse-debloat.sh"),
        content_type='text/plain',
        headers={'Content-Disposition': 'attachment; filename="echomuse-debloat.sh"'},
    )


@auth.require_admin
async def _get_provision_debloat_packages(request: web.Request) -> web.Response:
    """GET /api/provision/debloat_packages — the pm-hide package list as JSON.

    Parsed server-side (comments/blank lines stripped) so the wizard never
    has to understand the file format and list edits ship without a
    dashboard rebuild.
    """
    packages = [
        line.strip()
        for line in _read_payload("debloat_packages.txt").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return em_http.ok({"packages": packages})


@auth.require_admin
async def _get_provision_latest_binary(request: web.Request) -> web.Response:
    """
    GET /api/provision/latest_binary — serves the bytes of the latest
    GitHub release binary, for the provisioning wizard's "Install latest
    from GitHub" step.

    Distinct from /api/releases/latest (metadata only: {version, url}) —
    this route does the actual download from GitHub on the server side
    and streams the binary back, since a freshly-flashed device isn't
    registered in em_state.devices yet and can't go through the
    /api/devices/{id}/update fleet-OTA path (that requires a live
    WebSocket session). Reuses the same cache/fetch machinery as OTA.
    """
    release = await em_releases.cached_firmware()
    if release is None:
        return em_http.error("no_release", "No release information available", 404)

    binary = await em_releases.download(release["url"], release.get("version", ""))
    if binary is None:
        return em_http.error("fetch_failed", "Could not download binary from GitHub", 502)

    return web.Response(
        body=binary,
        content_type='application/octet-stream',
        headers={
            'Content-Disposition': 'attachment; filename="server"',
            'X-Release-Version': release["version"],
        },
    )


@auth.require_admin
async def _get_provision_magisk_db(request: web.Request) -> web.Response:
    """GET /api/provision/magisk_db — generates a pre-seeded Magisk grant DB.

    Grants uid 2000 (adb shell) and uid 0 (root) unconditional su access so
    the screenless Echo Dot never shows a grant dialog.
    """
    def _build_db() -> bytes:
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            con = _sqlite3.connect(path)
            # Schema confirmed against a real Magisk v17.3 device dump
            # (sqlite> .schema on a working /data/adb/magisk.db) — NOT
            # guessed. magiskd queries settings and strings on every su
            # request regardless of whether anything's stored in them;
            # the previous version of this function only created
            # `policies` (and with the wrong columns — no package_name in
            # the real schema, PRIMARY KEY is uid alone). Missing
            # settings/strings meant every single su call hit
            # "sqlite3_exec: no such table" on each of those two tables
            # and got hard-rejected — which looked like a hang from the
            # wizard side because su was taking up to ~60s per rejection
            # cycle, far longer than the wizard's retry loop accounted for.
            con.execute(
                "CREATE TABLE policies ("
                "  uid INT,"
                "  policy INT,"
                "  until INT,"
                "  logging INT,"
                "  notification INT,"
                "  PRIMARY KEY(uid)"
                ")"
            )
            con.execute(
                "CREATE TABLE settings (key TEXT, value INT, PRIMARY KEY(key))"
            )
            con.execute(
                "CREATE TABLE strings (key TEXT, value TEXT, PRIMARY KEY(key))"
            )
            con.execute(
                "CREATE TABLE denylist (package_name TEXT, process TEXT, "
                "PRIMARY KEY(package_name, process))"
            )
            # policy=2 → always grant. Matches the confirmed real schema:
            # uid, policy, until, logging, notification — no package_name.
            con.execute("INSERT INTO policies (uid, policy, until, logging, notification) VALUES (2000, 2, 0, 1, 1)")
            con.execute("INSERT INTO policies (uid, policy, until, logging, notification) VALUES (0, 2, 0, 1, 1)")
            con.commit()
            con.close()
            return Path(path).read_bytes()
        finally:
            os.unlink(path)

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _build_db)
    return web.Response(
        body=data,
        content_type='application/octet-stream',
        headers={'Content-Disposition': 'attachment; filename="magisk.db"'},
    )


@auth.require_auth
async def _get_provision_oww_manifest(request: web.Request) -> web.Response:
    """
    GET /api/provision/oww_assets — what the wizard should push, and where.

    The wizard pushes over ADB from the browser rather than through the shell
    plane: a freshly-flashed device is not in em_state.devices yet, and USB is far
    better suited to 15MB than a base64 heredoc. Same bytes, same md5s, same
    destination as the field path — only the transport differs.
    """
    fleet = await asyncio.get_running_loop().run_in_executor(
        None, db.get_global_device_config) or {}
    models = [m for m in [fleet.get("owwModel") or ""] if m]
    desired, problems = em_oww_assets.desired_assets(models)
    return em_http.ok({
        "dir": em_oww_assets.DEVICE_DIR,
        "problems": problems,
        "assets": [{"name": a.name, "size": a.size, "md5": a.md5} for a in desired],
    })


@auth.require_auth
async def _get_provision_oww_asset(request: web.Request) -> web.Response:
    """
    GET /api/provision/oww_asset/{name} — the bytes of one asset.

    Serves only names the manifest just listed, resolved from the manifest
    rather than from the request: the filename is user-supplied, and joining
    it onto a directory is how a path traversal gets written by accident.
    """
    name = request.match_info["name"]
    fleet = await asyncio.get_running_loop().run_in_executor(
        None, db.get_global_device_config) or {}
    desired, _ = em_oww_assets.desired_assets(
        [m for m in [fleet.get("owwModel") or ""] if m])
    asset = next((a for a in desired if a.name == name), None)
    if asset is None:
        return em_http.error("not_found", f"{name} is not a current asset", 404)
    try:
        data = asset.source.read_bytes()
    except OSError as e:
        return em_http.error("unreadable", str(e), 500)
    return web.Response(
        body=data,
        content_type="application/octet-stream",
        headers={"X-Asset-MD5": asset.md5},
    )


@auth.require_admin
async def _post_provision_diagnostics(request: web.Request) -> web.Response:
    """
    POST /api/provision/diagnostics

    The wizard collects raw probe output when a step fails and posts it here;
    this returns the sanitised file to attach to an issue (#87).

    Packaged on the controller rather than in the browser on purpose. The
    redaction rules and their tests live in em_support, and a second copy in
    JavaScript would drift from them without anyone noticing until a file
    carried an SSID. This function only carries; em_support decides.

    Admin-only and a download rather than a display, the same call the support
    bundle makes: it is meant to be looked at before it is shared.
    """
    body = await em_http.json_body(request)
    diag = em_support.build_provision_diagnostics(
        step=body.get("step") or "unknown",
        error=body.get("error") or "",
        probes=body.get("probes") or {},
        transcript=body.get("transcript") or None,
        # The wizard knows which network the operator picked; the file cannot
        # work it out once the names are gone, and "the one you wanted is
        # WPA3" is the whole answer on a #82-shaped failure.
        selected_ssid=body.get("selected_ssid") or None,
        controller_version=CONTROLLER_VERSION,
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return web.Response(
        body=em_support.to_json(diag).encode(),
        content_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="echomuse-provision-{stamp}.json"'},
    )


@auth.require_admin
async def _get_provision_emos_init(request: web.Request) -> web.Response:
    """
    GET /api/provision/emos_init — the emOS init binary from the latest
    emOS release, so the wizard does not need one chosen by hand.

    Downloaded server-side for the reason `latest_binary` is: the device being
    provisioned is not registered yet, and the browser cannot reach GitHub
    under the dashboard's CSP.

    **The init is the ONLY part of an emOS image we can distribute.** A
    bootable image contains the device's own kernel and device trees, so
    shipping one would mean redistributing Amazon's code; the image is
    assembled from the boot partition the user read off their own device.

    Verified before it is served, not after it is flashed. The same checks the
    build applies, run here as well, so a release built wrong is refused at the
    point of download rather than at the point of boot.

    `?arch=arm|arm64` picks which init, because it must match the device's
    KERNEL and the two FireOS versions differ. It DEFAULTS to arm64, which is
    what this served when there was only one asset — so an older dashboard, or
    anyone fetching the file by hand, keeps getting the FireOS 5 init.

    A caller that holds the reference image should not use this at all: POST the
    reference to `/api/provision/emos_image` with `use_latest_init` and let the
    controller read the architecture off it. Passing an arch means the caller
    decided, and the only thing that actually knows is the image.
    """
    arch = (request.query.get("arch") or em_emos_build.ARCH_ARM64).strip()
    if arch not in em_emos_ota.INIT_ASSETS:
        return em_http.error(
            "bad_arch",
            f"Unknown architecture {arch!r} — expected one of "
            f"{', '.join(sorted(em_emos_ota.INIT_ASSETS))}.", 400)

    binary, version, err = await em_emos_ota.fetch_init(arch)
    if err is not None:
        return err

    return web.Response(
        body=binary,
        content_type="application/octet-stream",
        headers={
            # Named for the asset that was served, so a downloaded file says
            # which kernel it is for rather than every arch arriving as "init".
            "Content-Disposition":
                f'attachment; filename="{em_emos_ota.INIT_ASSETS[arch]}"',
            "X-Emos-Version": version,
            "X-Emos-Arch": arch,
        },
    )


# The multipart fields _post_provision_emos_image reads, and the only ones. A
# field the wizard sends that is not named here is dropped without a word.
#
# That is how `system_part` went missing for the life of the feature (#545).
# The wizard resolved it, logged which partition it had chosen, and appended
# it; the loop below had no branch for it, so `parts` never carried it, the
# validation that follows could not fire, and every v2 image was built with no
# `emos.system=` stamp — while the release notes, the wizard transcript and
# this file all said otherwise. emOS then fell back to its hardcoded p13, which
# is the right partition about half the time.
#
# tests/test_emos_image_fields.py compares this against what dashboard.jsx
# appends to the same POST, so the next field to be added has to be read here
# or fail CI.
EMOS_IMAGE_FIELDS = ("reference", "init", "reference_md5", "version",
                     "use_latest_init", "system_part", "board",
                     "reference_upload", "init_upload")

# Uploads sent in pieces by the wizard, claimed by the build that names them.
_chunks = em_chunked.ChunkStore()


@auth.require_admin
async def _post_provision_upload_chunk(request: web.Request) -> web.Response:
    """
    POST /api/provision/upload_chunk?id=&offset=&total= (raw body)

    One piece of an upload too large to cross a proxy in one request (#800).
    """
    q = request.query
    try:
        offset, total = int(q.get("offset", "")), int(q.get("total", ""))
    except ValueError:
        return em_http.error("bad_chunk", "offset and total must be integers", 400)
    if (request.content_length or 0) > em_chunked.CHUNK_MAX:
        return em_http.error("bad_chunk", f"a chunk may be at most "
                             f"{em_chunked.CHUNK_MAX} bytes", 413)
    data = await request.read()
    try:
        held = _chunks.put(q.get("id", ""), offset, total, data)
    except em_chunked.ChunkError as e:
        return em_http.error("bad_chunk", str(e), 400)
    return web.json_response({"received": held})


async def _read_emos_image_parts(request: web.Request) -> dict:
    """The build POST's multipart fields, with named uploads claimed."""
    reader = await request.multipart()
    parts = {}
    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name not in EMOS_IMAGE_FIELDS:
            # Loud, because the silent version of this cost every v2
            # device its /system stamp.
            log.warning(f"[api] emOS image: ignoring multipart field "
                        f"{field.name!r}, which this endpoint does not read")
            continue
        if field.name in ("reference", "init"):
            parts[field.name] = await field.read()
        elif field.name in ("reference_upload", "init_upload"):
            upload_id = (await field.read()).decode(errors="replace")[:64].strip()
            # Absent or incomplete reads as not sent, which the checks below
            # refuse by name.
            data = _chunks.take(upload_id)
            if data is not None:
                parts[field.name.removesuffix("_upload")] = data
        elif field.name == "system_part":
            parts["system_part"] = (await field.read()).decode(
                errors="replace")[:8]
        elif field.name == "board":
            # The device's own identity (_echoBoard); radar's kernel is patched
            # to keep the ramdisk. Absent from a wizard older than radar.
            parts["board"] = (await field.read()).decode(
                errors="replace")[:32].strip()
        elif field.name == "reference_md5":
            parts["reference_md5"] = (await field.read()).decode(
                errors="replace")[:64].strip().lower()
        elif field.name == "version":
            parts["version"] = (await field.read()).decode(errors="replace")[:64]
        elif field.name == "use_latest_init":
            parts["use_latest_init"] = (await field.read()).decode(
                errors="replace")[:8].strip() not in ("", "0", "false")
    return parts


@auth.require_admin
async def _post_provision_emos_image(request: web.Request) -> web.Response:
    """
    POST /api/provision/emos_image (multipart: EMOS_IMAGE_FIELDS)

    Build an emOS boot image from the reference the wizard just escrowed off
    the device, and stream it back. Step 5 of the emOS provisioning flow.

    ON THE CONTROLLER RATHER THAN IN THE BROWSER, for the reason the
    diagnostics route above gives: the packer and its refusals live in
    em_emos_build, with tests, and a second copy in JavaScript would drift
    from them without anyone noticing until a device took a bad flash. This
    function only carries; em_emos_build decides.

    NOTHING IS STORED. The reference is the user's own boot partition and the
    only copy that matters is the one the wizard escrowed to them — keeping a
    second here would mean holding a device image we have no reason to hold,
    and it is also the file we take care never to redistribute. Same reason
    the built image is streamed rather than cached.

    `use_latest_init` resolves the init HERE, because the init must match the
    reference's kernel and this is the only place holding the reference. A caller
    choosing for itself would need a second copy of reference_kernel_arch.

    An explicit `init` part still wins, for a hand-built binary.
    """
    try:
        parts = await _read_emos_image_parts(request)
        reference = parts.get("reference")
        init_bin = parts.get("init")
        if not reference:
            return em_http.error("invalid_upload",
                          "Expected multipart field 'reference' — the boot "
                          "image read off the device — or a complete "
                          "'reference_upload'", 400)
        if not init_bin and not parts.get("use_latest_init"):
            return em_http.error("invalid_upload",
                          "Expected multipart field 'init' — the emOS init "
                          "binary — or 'use_latest_init' to resolve it here",
                          400)

        # The escrow arrived intact, checked before anything reads it.
        #
        # This replaces a property the packer's round-trip used to provide as a
        # side effect: the boot header carries a SHA1 over the kernel and
        # ramdisk, so a byte corrupted in transfer made the repack disagree and
        # was refused. That check had to be relaxed — some images legitimately
        # carry a stale id that cannot be reproduced by definition, and no rule
        # can tell a stale id from a corrupted byte — so the integrity half is
        # now explicit, and covers the WHOLE transfer rather than two of its
        # regions.
        #
        # Absent md5 is accepted: an older wizard does not send one, and
        # refusing there would break provisioning for a dashboard that has not
        # been reloaded. Present-and-wrong always refuses.
        want_md5 = parts.get("reference_md5")
        if want_md5:
            got_md5 = hashlib.md5(reference).hexdigest()
            if got_md5 != want_md5:
                return em_http.error(
                    "corrupt_upload",
                    f"The boot image arrived corrupted: the wizard read "
                    f"{want_md5} off the device and {got_md5} arrived. "
                    f"Nothing has been built. Re-run the escrow step.", 400)

        version = parts.get("version") or "0.1"
        # A hand-picked init carries no WiFi tools, matching emos/build.sh.
        sbin = {}

        # Which FireOS userspace this reference was read beside, stamped onto
        # the image so emOS mounts that one rather than assuming. The WIZARD
        # resolves it, because system_a/system_b are names and TWRP's by-name
        # map is the only place those names exist — this end never guesses.
        #
        # Absent is allowed: an image with no stamp falls back to the partition
        # emOS hardcoded before this existed, so an older wizard keeps working.
        # A value we cannot read is refused rather than dropped, because
        # silently omitting it builds an image that mounts the wrong userspace
        # and boots.
        system_part = None
        raw_part = (parts.get("system_part") or "").strip()
        if raw_part:
            if not raw_part.isdigit() or not 1 <= int(raw_part) <= 127:
                return em_http.error(
                    "bad_system_part",
                    f"system_part must be an mmcblk0 partition number (1-127), "
                    f"not {raw_part!r}. Nothing has been built.", 400)
            system_part = int(raw_part)

        # Also keeps ~3.5MB out of a request that has already hit HA ingress's
        # 413 once (2026-09-06).
        if parts.get("use_latest_init"):
            arch = em_emos_build.reference_kernel_arch(reference)
            if not arch:
                # Refused, not defaulted: an init chosen by guess flashes fine
                # and then produces no output at all.
                return em_http.error(
                    "unknown_reference_arch",
                    "Could not read the kernel architecture out of that boot "
                    "image, so there is no way to tell which init it needs. "
                    "Check the escrow is the whole boot image, or build an init "
                    "from emos/ with build.sh and select it by hand.", 400)
            init_bin, sbin, init_version, err = await em_emos_ota.fetch_payload(arch, parts.get("board"))
            if err is not None:
                return err
            version = parts.get("version") or init_version
            log.info(f"[api] emOS image: reference kernel is {arch}, using "
                     f"{em_emos_ota.INIT_ASSETS[arch]} from {init_version}"
                     + (f" plus {', '.join(sorted(sbin))}" if sbin else ""))

        loop = asyncio.get_event_loop()
        # Off the event loop: gzipping a ramdisk and hashing two images blocks
        # it for long enough to matter, and devices are streaming audio
        # through this process while somebody provisions a new one.
        info = await loop.run_in_executor(
            None, em_emos_build.build_emos_image, reference, init_bin, version,
            "", sbin, system_part, parts.get("board") or None)

        log.info(f"[api] emOS image built"
                 # Not "(older wizard)", which is one of three ways to get
                 # here and was the wrong one when this line last mattered:
                 # the v1 path sends no partition by design, and until #545
                 # the handler dropped the one the wizard did send. State
                 # what is true — no stamp — and leave the cause alone.
                 + (f" for /system on p{system_part}" if system_part else
                    " with no /system stamp")
                 + f": {info['size']:,} bytes "
                 f"md5={info['md5'][:8]}… from a {info['reference_size']:,} "
                 f"byte reference (md5 {info['reference_md5'][:8]}…)")

        image = info.pop("image")
        return web.Response(
            body=image,
            content_type="application/octet-stream",
            headers={
                "Content-Disposition": 'attachment; filename="emos-boot.img"',
                # The wizard compares this against its own hash of what it
                # received, and again against what it reads back off the
                # device after the flash. Both comparisons are the point of
                # the step.
                "X-Image-MD5": info["md5"],
                "X-Image-SHA256": info["sha256"],
                "X-Reference-MD5": info["reference_md5"],
                "X-Build-Info": json.dumps(info),
            },
        )
    except em_emos_build.BuildError as e:
        # Every one of these is a refusal with something a person can act on,
        # and each is a state we would rather meet here than after a partition
        # write. 422 rather than 400: the request was well formed, the image
        # is what could not be accepted.
        log.warning(f"[api] emOS build refused: {e}")
        return em_http.error("build_refused", str(e), 422)
    except web.HTTPException:
        raise
    except Exception as e:
        log.error(f"[api] emOS build error: {e}")
        return em_http.error("build_failed", str(e), 500)
