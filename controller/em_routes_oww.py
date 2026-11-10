"""
Wake word routes: the custom openWakeWord models in the data volume (list,
upload, delete), and the on-device runtime and models installed on one Echo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from aiohttp import web

import em_auth as auth
import em_db as db
import em_http
import em_oww_assets
import em_oww_check
import em_oww_models
import em_oww_sync
import em_state

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    # Custom wake-word models (oww_forge output → data/oww_models/)
    app.router.add_get("/api/oww_models",             _get_oww_models)
    app.router.add_post("/api/oww_models/upload",     _post_oww_model_upload)
    app.router.add_delete("/api/oww_models/{file}",   _delete_oww_model)
    app.router.add_get("/api/devices/{id}/oww_assets",    _get_oww_assets)
    app.router.add_post("/api/devices/{id}/oww_assets",   _post_oww_assets)


@auth.require_auth
async def _get_oww_models(request: web.Request) -> web.Response:
    """
    GET /api/oww_models

    Custom models discovered in the data volume's oww_models/ dir.
    `path` is the value to store in owwModel config.
    """
    return em_http.ok({
        "models": em_oww_models.scan(),
        "dir":    str(em_oww_models.models_dir()),
    })


@auth.require_admin
async def _post_oww_model_upload(request: web.Request) -> web.Response:
    """
    POST /api/oww_models/upload (multipart: field name "model", .onnx file)

    Installs an openWakeWord model into the persisted models dir. The
    file lands atomically (tmp + rename) so a wake-listener reload can
    never see a half-written model.
    """
    try:
        reader = await request.multipart()
        field  = await reader.next()
        if field is None or field.name != "model":
            return em_http.error("invalid_upload", "Expected multipart field 'model'", 400)
        fname = em_oww_models.safe_model_filename(field.filename or "")
        if fname is None:
            return em_http.error("invalid_filename",
                          "Model must be a .onnx file with a simple name "
                          "(letters, digits, _ - . only)", 400)
        data = await field.read()
        if not data:
            return em_http.error("empty_upload", "Uploaded model is empty", 400)
        if len(data) > em_oww_models.MAX_MODEL_BYTES:
            return em_http.error("too_large", "Model exceeds 20 MB limit", 413)

        # Before anything is written: a model that cannot run is refused with
        # the reason, where it used to fail later in the wake listener (#869).
        why = await asyncio.get_running_loop().run_in_executor(
            None, em_oww_check.problem, data)
        if why:
            log.info(f"[api] Wake model refused: {fname}: {why}")
            return em_http.error("invalid_model",
                          f"This file cannot run as a wake word model: {why}.", 400)

        directory = em_oww_models.models_dir()
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, directory / fname)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        log.info(f"[api] Wake model installed: {fname} ({len(data):,} bytes)")
        entry = next((m for m in em_oww_models.scan() if m["file"] == fname), None)
        return em_http.ok({"model": entry}, status=201)
    except Exception as e:
        log.error(f"[api] Model upload error: {e}")
        return em_http.error("upload_failed", str(e), 500)


@auth.require_admin
async def _delete_oww_model(request: web.Request) -> web.Response:
    """
    DELETE /api/oww_models/{file}

    Refuses (409) while any device config or the global default still
    points at the model — deleting under a live listener would fail its
    next reload.
    """
    fname = em_oww_models.safe_model_filename(request.match_info["file"])
    if fname is None:
        return em_http.error("invalid_filename", "Bad model filename", 400)
    path = em_oww_models.models_dir() / fname
    if not path.is_file():
        return em_http.error("not_found", "No such model", 404)

    def _configs() -> dict[str, dict]:
        out = {"global": db.get_global_device_config()}
        for row in db.get_all_devices():
            out[row["device_id"]] = db.get_device_config(row["device_id"])
        return out

    configs = await asyncio.get_running_loop().run_in_executor(None, _configs)
    users = em_oww_models.in_use_by(str(path), configs)
    if users:
        return em_http.error("model_in_use",
                      f"Model is selected by: {', '.join(users)}", 409)

    path.unlink()
    log.info(f"[api] Wake model deleted: {fname}")
    return em_http.ok({"deleted": fname})


@auth.require_auth
async def _get_oww_assets(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/oww_assets — what is installed, and what is needed."""
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)

    desired, problems = em_oww_assets.desired_assets(em_oww_sync.wanted_models(device_id))
    payload = {
        "device_dir": em_oww_assets.DEVICE_DIR,
        "problems": problems,
        "required": [
            {"name": a.name, "kind": a.kind, "size": a.size, "md5": a.md5}
            for a in desired
        ],
        "connected": live is not None,
    }
    if live is None:
        # Not an error: the state is simply unknowable, and saying "not
        # installed" for an offline device would be a guess that reads as fact.
        payload.update({"status": "unknown", "installed": None, "free_mb": None})
        return em_http.ok(payload)

    state = await em_oww_sync.device_state(live)
    plan = em_oww_assets.plan_sync(desired, state["installed"], state["free_mb"])
    payload.update({
        "installed": {k: v[0] for k, v in state["installed"].items()},
        "free_mb": state["free_mb"],
        "missing": [a.name for a in plan.push],
        "prunable": plan.prune,
        "blocked": plan.blocked,
        "status": ("blocked" if plan.blocked
                   else "installed" if plan.is_noop
                   else "outdated" if plan.keep
                   else "absent"),
    })
    return em_http.ok(payload)


@auth.require_admin
async def _post_oww_assets(request: web.Request) -> web.Response:
    """POST /api/devices/{id}/oww_assets — install or update them."""
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)
    result = await em_oww_sync.sync(live, device_id)
    if not result.get("ok"):
        return em_http.error("sync_failed", result.get("error", "sync failed"), 500)
    if result.get("pushed") and em_state.devices.get(device_id) is live:
        await em_oww_sync.push_config(device_id, live)
    return em_http.ok(result)
