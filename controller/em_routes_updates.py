"""
The Updates tab's routes: firmware update and rollback, uploading a local
firmware build or emOS payload, the emOS update, the latest firmware release
and deploying it fleet-wide, and the controller release notice.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

import em_auth as auth
import em_db as db
import em_emos_build
import em_emos_ota
import em_emos_update
import em_http
import em_ota
import em_payloads
import em_releases
import em_state
import em_tasks
from version import VERSION as CONTROLLER_VERSION
from version import compare as _compare_versions

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    app.router.add_post("/api/devices/{id}/update",       _post_device_update)
    app.router.add_post("/api/devices/{id}/emos_update",  _post_device_emos_update)
    app.router.add_post("/api/emos/upload",               _post_emos_upload)
    app.router.add_post("/api/devices/{id}/rollback",     _post_device_rollback)
    app.router.add_post("/api/releases/upload",           _post_upload_binary)
    app.router.add_get("/api/releases/latest",   _get_latest_release)
    app.router.add_get("/api/releases/controller", _get_controller_release)
    app.router.add_post("/api/releases/check",   _post_check_release)
    app.router.add_post("/api/releases/deploy",  _post_deploy_all)
    app.router.add_post("/api/devices/{id}/debloat",      _post_debloat)


# Pending local binary uploads — keyed by UUID token, expire after 10 minutes.
_pending_uploads: dict[str, bytes] = {}


# Uploaded payload bundles (the dev path), token -> (files, version).
_pending_emos_uploads: dict[str, tuple] = {}


# Largest firmware binary /api/releases/upload will accept. Roughly 5x the
# current ~10.7 MB build, so it bounds memory (the upload is held in RAM until
# deployed or expired) without needing revision every release. The aiohttp
# transport limit is set ABOVE this in create_app so this is the ceiling a
# user actually meets, with a message that names it.
UPLOAD_MAX_BYTES = 50 * 1024 * 1024


@auth.require_admin
async def _post_device_update(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/update

    Deploys a new binary to the device using A/B slots.
    Accepts an optional JSON body with {"upload_token": "..."} to deploy a
    locally uploaded binary instead of the latest GitHub release.

    Returns 202 Accepted — update runs in the background.
    """
    device_id = request.match_info["id"]

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    upload_token = body.get("upload_token")
    binary_override = None
    release = None

    if upload_token:
        # PEEKED, not popped. Every refusal below leaves the token usable, so
        # re-deciding — or forcing — costs nothing. Popping first meant an
        # 11MB re-upload to get past a check that had just told the operator
        # something useful.
        binary_override = _pending_uploads.get(upload_token)
        if binary_override is None:
            return em_http.error("invalid_token", "Upload token not found or expired", 404)
        _embedded = em_ota.extract_binary_version(binary_override)
        _ver      = _embedded or f"local-{time.strftime('%Y%m%d-%H%M')}"
        release   = {"version": _ver, "url": None}
    else:
        release = await em_releases.cached_firmware()
        if release is None:
            return em_http.error("no_release", "No release information available", 409)

    loop = asyncio.get_event_loop()
    row  = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)

    if (device_id in em_ota.in_progress or device_id in em_ota.queued
            or em_emos_ota.busy(device_id)):
        return em_http.error("update_in_progress", "An update is already in progress", 409)

    # Installing what the device is already running costs a transfer, a
    # reboot and a slot — and the fleet endpoint has always skipped it for
    # releases (`already_current`), while this endpoint checked nothing and
    # the upload path could not check anything, because it labelled every
    # uploaded binary `local-<timestamp>` instead of reading the version out
    # of it. So the case most likely to happen by accident was the one case
    # nothing guarded: an engineering build uploaded by hand, twice.
    #
    # Refused rather than silently skipped, because someone pressed a button
    # here and a no-op reported as success is how they press it again.
    # `force` exists because re-flashing the SAME version is a real repair —
    # a corrupt slot is fixed by writing it again — so this must never become
    # a wall between an operator and their own device.
    if (release["version"] and row["firmware_ver"] == release["version"]
            and not body.get("force")):
        return em_http.error(
            "already_running",
            f"This device is already running {release['version']}. "
            f"Pass force to install it again.",
            409,
        )

    if upload_token:
        _pending_uploads.pop(upload_token, None)
    em_tasks.spawn(em_ota.run_update(device_id, release, binary_override))
    return em_http.ok({"status": "started", "version": release["version"]}, status=202)


@auth.require_admin
async def _post_device_rollback(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/rollback

    Flips the inactive A/B slot back to active. Instant — no binary transfer.
    Requires firmware_previous to be set.
    Returns 202 Accepted.
    """
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row  = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    if not row["firmware_previous"]:
        return em_http.error("no_rollback_available",
                      "No previous version recorded — cannot roll back", 404)

    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)

    if (device_id in em_ota.in_progress or device_id in em_ota.queued
            or em_emos_ota.busy(device_id)):
        return em_http.error("update_in_progress", "An update is already in progress", 409)

    em_tasks.spawn(em_ota.run_rollback(device_id, row["firmware_previous"]))
    return em_http.ok({"status": "started", "rolling_back_to": row["firmware_previous"]}, status=202)


@auth.require_admin
async def _post_upload_binary(request: web.Request) -> web.Response:
    """
    POST /api/releases/upload (multipart: field name "binary")

    Upload a local binary for deployment. Returns an upload_token valid for
    10 minutes. Pass the token to /api/devices/{id}/update or
    /api/releases/deploy to deploy it.
    """
    import uuid as _uuid
    try:
        reader = await request.multipart()
        field  = await reader.next()
        if field is None or field.name != "binary":
            return em_http.error("invalid_upload", "Expected multipart field 'binary'", 400)
        binary = await field.read()
        if not binary:
            return em_http.error("empty_upload", "Uploaded binary is empty", 400)
        if len(binary) > UPLOAD_MAX_BYTES:
            return em_http.error(
                "too_large",
                f"Binary is {len(binary) / 1024 / 1024:.1f} MB, over the "
                f"{UPLOAD_MAX_BYTES // 1024 // 1024} MB limit",
                413,
            )

        token = str(_uuid.uuid4())
        _pending_uploads[token] = binary
        version = em_ota.extract_binary_version(binary)
        log.info(f"[api] Binary uploaded: {len(binary):,} bytes "
                 f"version={version or 'unknown'} token={token[:8]}…")

        async def _expire():
            await asyncio.sleep(600)
            _pending_uploads.pop(token, None)
        em_tasks.spawn(_expire())

        # The version rides back so the dashboard can say what was uploaded,
        # and warn against a device already running it BEFORE the operator
        # commits. The update endpoint refuses it either way; this is what
        # makes the refusal something you see coming instead of something you
        # find out afterwards. None means the binary carries no recognisable
        # version — shown as unknown rather than guessed at.
        return em_http.ok({"upload_token": token, "size": len(binary),
                    "version": version})
    except web.HTTPException:
        # aiohttp's own — HTTPRequestEntityTooLarge above all, raised by the
        # transport before this handler sees a byte. Swallowing it into a 500
        # is how a 1 MB transport limit presented as "an internal error
        # occurred" instead of naming a size, and the middleware already
        # re-raises these deliberately.
        raise
    except Exception as e:
        log.error(f"[api] Upload error: {e}")
        return em_http.error("upload_failed", str(e), 500)


# ─── emOS update (#573) ───────────────────────────────────────────────────────
#
# Same shape as the firmware update above — per device, queued behind
# `em_ota.queue`, verified before anything is written, watched afterwards — but
# what it writes is the boot partition. em_emos_update holds every decision
# and its docstring is the design; this section only carries.


@auth.require_admin
async def _post_emos_upload(request: web.Request) -> web.Response:
    """
    POST /api/emos/upload (multipart: field "payload", an emos-payload.zip)

    The emOS counterpart of /api/releases/upload: a locally built payload
    bundle (emos/tools/make-payload-bundle.py), so an init can be tried on a
    bench device before it is tagged. Returns an upload_token for
    /api/devices/{id}/emos_update, valid for 10 minutes.
    """
    import uuid as _uuid
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "payload":
            return em_http.error("invalid_upload",
                          "Expected multipart field 'payload'", 400)
        raw = await field.read()
    except web.HTTPException:
        raise
    except Exception as e:
        log.error(f"[api] emOS payload upload error: {e}")
        return em_http.error("upload_failed", str(e), 500)
    if not raw:
        return em_http.error("empty_upload", "Uploaded payload is empty", 400)
    if len(raw) > UPLOAD_MAX_BYTES:
        return em_http.error("too_large", "Payload is over the upload limit", 413)
    try:
        payload = await asyncio.get_event_loop().run_in_executor(
            None, em_emos_build.read_payload_bundle, raw)
    except em_emos_build.BuildError as e:
        return em_http.error("bad_payload", f"That is not a usable emOS payload: {e}", 400)

    version = payload["version"] or f"local-{time.strftime('%Y%m%d-%H%M')}"
    token = str(_uuid.uuid4())
    _pending_emos_uploads[token] = (payload["files"], version)
    log.info(f"[api] emOS payload uploaded: {len(raw):,} bytes "
             f"version={version} files={sorted(payload['files'])}")

    async def _expire():
        await asyncio.sleep(600)
        _pending_emos_uploads.pop(token, None)
    em_tasks.spawn(_expire())
    return em_http.ok({"upload_token": token, "version": version,
                "files": sorted(payload["files"])})


@auth.require_admin
async def _post_device_emos_update(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/emos_update

    Rebuild the device's emOS image around the latest release's init and flash
    it. Optional JSON body: {"upload_token": "..."} to use an uploaded payload
    instead, {"force": true} to install a release the device is not behind.

    Returns 202 — the update runs in the background.
    """
    device_id = request.match_info["id"]
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)
    # Refused here as well as greyed out in the panel: this is a plain POST.
    if live.android_userspace:
        return em_http.error("not_emos", "This device is not running emOS", 409)
    if (em_emos_ota.busy(device_id) or device_id in em_ota.in_progress
            or device_id in em_ota.queued):
        return em_http.error("update_in_progress", "An update is already in progress", 409)

    override = None
    token = body.get("upload_token")
    if token:
        override = _pending_emos_uploads.get(token)
        if override is None:
            return em_http.error("invalid_token", "Upload token not found or expired", 404)
        version = override[1]
    else:
        release = await em_releases.fetch_emos()
        if release is None:
            return em_http.error("no_release", "No emOS release information available", 409)
        version = release["version"]
        if not em_emos_update.can_target(version):
            return em_http.error(
                "release_too_old",
                f"emOS {version} predates updating over the network: its init "
                f"cannot roll a failed update back by itself.", 409)
        if (not em_emos_update.update_available(row["emos_version"], version)
                and not body.get("force")):
            return em_http.error(
                "already_running",
                f"This device runs emOS {row['emos_version'] or 'unknown'}; "
                f"{version} is not an update for it. Pass force to install "
                f"it anyway.", 409)

    if token:
        _pending_emos_uploads.pop(token, None)
    em_tasks.spawn(em_emos_ota.run_update(device_id, override))
    return em_http.ok({"status": "started", "version": version}, status=202)


@auth.require_auth
async def _get_latest_release(request: web.Request) -> web.Response:
    """GET /api/releases/latest — latest GitHub release, from cache."""
    release = await em_releases.cached_firmware()
    if release is None:
        return em_http.error("no_release", "No release information available", 404)
    return em_http.ok(release)


@auth.require_admin
async def _post_check_release(request: web.Request) -> web.Response:
    """POST /api/releases/check — force re-poll GitHub."""
    release = await em_releases.fetch_firmware(force=True)
    # Same button, same question: the Updates tab shows emOS beside firmware.
    await em_releases.fetch_emos()
    if release is None:
        return em_http.error("no_release", "Could not fetch release from GitHub", 502)
    return em_http.ok(release)


@auth.require_admin
async def _post_deploy_all(request: web.Request) -> web.Response:
    """
    POST /api/releases/deploy

    Deploy to all connected, approved, non-current devices.
    Accepts optional {"upload_token": "..."} to deploy a local binary
    to the whole fleet instead of the latest GitHub release.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    upload_token = body.get("upload_token")
    binary_override = None
    release = None

    if upload_token:
        binary_override = _pending_uploads.pop(upload_token, None)
        if binary_override is None:
            return em_http.error("invalid_token", "Upload token not found or expired", 404)
        # Read the version OUT of the binary, as the single-device path does.
        # Labelling it `local-<timestamp>` made every uploaded binary look
        # like a version no device had ever run, so the already_current skip
        # below could never fire for an upload and the whole fleet re-flashed
        # what it was already running. The timestamp remains the fallback for
        # a binary carrying no recognisable version.
        _embedded = em_ota.extract_binary_version(binary_override)
        release = {
            "version": _embedded or f"local-{time.strftime('%Y%m%d-%H%M')}",
            "url": None,
        }
    else:
        release = await em_releases.cached_firmware()
        if release is None:
            return em_http.error("no_release", "No release information available", 409)

    started = []
    skipped = []
    loop = asyncio.get_event_loop()

    for device_id, live in list(em_state.devices.items()):
        row = await loop.run_in_executor(None, db.get_device, device_id)
        if row is None or not row["approved"]:
            skipped.append({"device_id": device_id, "reason": "not_approved"})
            continue
        # No longer `not upload_token and …`: an uploaded binary now carries a
        # real version, so this skip finally covers the fleet-wide re-flash of
        # an engineering build too. `force` overrides it for the repair case,
        # matching the single-device endpoint.
        if (release["version"] and row["firmware_ver"] == release["version"]
                and not body.get("force")):
            skipped.append({"device_id": device_id, "reason": "already_current"})
            continue
        if device_id in em_ota.in_progress or device_id in em_ota.queued:
            skipped.append({"device_id": device_id, "reason": "update_in_progress"})
            continue

        em_tasks.spawn(em_ota.run_update(device_id, release, binary_override))
        started.append(device_id)

    return em_http.ok({
        "version": release["version"],
        "started": started,
        "skipped": skipped,
    }, status=202)


@auth.require_auth
async def _get_controller_release(request: web.Request) -> web.Response:
    """GET /api/releases/controller"""
    data = await em_releases.fetch_controller()
    if data is None:
        # Fall back to the DB cache so a GitHub outage does not blank the
        # panel — offline is not the same as "no update exists".
        stored = await asyncio.get_running_loop().run_in_executor(None, lambda: {
            key: db.get_config(key, "") or "" for key in (
                "latest_controller_version", "latest_controller_notes",
                "latest_controller_published_at")})
        version = stored["latest_controller_version"]
        if not version:
            return em_http.ok({"version": None, "current": CONTROLLER_VERSION,
                        "status": "unknown", "available": False})
        data = {
            "version":      version,
            "current":      CONTROLLER_VERSION,
            "notes":        stored["latest_controller_notes"],
            "published_at": stored["latest_controller_published_at"],
            "release_url":  "",
            **_compare_versions(CONTROLLER_VERSION, version),
        }
    return em_http.ok(data)


@auth.require_admin
async def _post_debloat(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/debloat

    Re-apply the debloat payloads to a live device: sync the boot script and
    hide any newly-listed packages.

    This exists because the OTA-time sync cannot reach every device. A device
    already running the latest firmware will not be updated again, so it would
    never receive a payload change — which is exactly the situation the first
    device hit (Lounge was current when round 2 landed). Idempotent, so
    pressing it twice costs a `pm list packages` and nothing else.
    """
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", f"Device not connected: {device_id}", 409)

    # Refused server-side, not merely greyed out in the dashboard. This is a
    # plain POST with a session token, so a dashboard-only rule protects
    # nothing from anyone who opens the network tab — the same reasoning that
    # puts the recordings check on the server. And the cost of running it
    # anyway is not a no-op: the payload targets Magisk's /sbin/.core overlay,
    # which a device without Magisk has no daemon to create, so the write
    # fails, TRANSFER_OK never comes, and the transfer holds that device's
    # shell lock for its full timeout.
    if not live.android_userspace:
        return em_http.error(
            "not_android",
            "This device is not running Android, so there is nothing to "
            "debloat — the payload is a package list and a Magisk boot script.",
            409)

    # No explicit shell release here: em_shell.run and em_shell.stream_file each
    # acquire and release the session in their own finally, which is why
    # em_payloads.sync_start_script does not either. Releasing it from out here could close
    # a session a concurrent caller had opened.
    em_tasks.spawn(em_payloads.sync_debloat(live, device_id))
    return em_http.ok({"started": True})
