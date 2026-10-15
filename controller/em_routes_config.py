"""
Fleet config routes: the global device config every Echo inherits for the
sections it does not override, read and saved.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

import em_auth as auth
import em_ble_proxy
import em_config_types
import em_db as db
import em_device_config
import em_endpoints
import em_http
import em_payloads
import em_reconcile
import em_state
import em_tasks

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    app.router.add_get("/api/global/config",   _get_global_config)
    app.router.add_post("/api/global/config",  _post_global_config)


@auth.require_auth
async def _get_global_config(request: web.Request) -> web.Response:
    """GET /api/global/config — fleet-wide default device config."""
    loop = asyncio.get_event_loop()
    config = await loop.run_in_executor(None, db.get_global_device_config)
    return em_http.ok(em_device_config.redact_console_pw(config))


@auth.require_admin
async def _post_global_config(request: web.Request) -> web.Response:
    """
    POST /api/global/config

    Persists new fleet-wide device defaults, then pushes each connected
    device its freshly resolved EFFECTIVE config.

    Since v8 that means every connected device, not just fully-inheriting
    ones: a device overriding only Ring still follows the fleet for
    Microphones, Wake word and the rest, so it has to receive this change.
    Each device gets its own resolved config rather than the raw body —
    sending the body would blow away exactly the overrides being respected.

    The body REPLACES the stored config. A body that would drop existing
    keys is refused with 409 unless it sets replace=true — see
    em_device_config.dropped_keys.
    """
    config = await em_http.json_body(request)
    loop = asyncio.get_event_loop()

    explicit_replace = bool(config.pop("replace", False))
    # Raw (defaults NOT underlaid): see get_global_device_config_raw — a
    # newly-added default must not look like a key this body is deleting.
    stored = await loop.run_in_executor(None, db.get_global_device_config_raw)
    em_device_config.resolve_console_pw(config, stored)
    if (err := em_device_config.validate_console_timeout(config)):
        return em_http.error("bad_console_timeout", err, 400)
    if (bad := em_config_types.problems(config, stored)):
        return em_http.error("bad_config_value", "; ".join(bad), 400)
    endpoints_before = stored.get("controllerEndpoints") or []
    if "controllerEndpoints" in config:
        eps, err = em_endpoints.normalise(
            config["controllerEndpoints"], em_state.link["port"], em_state.link["tls_port"])
        if err:
            return em_http.error("bad_controller_endpoints", f"Controller address list {err}.", 400)
        config["controllerEndpoints"] = eps
    dropped = em_device_config.dropped_keys(config, stored)
    if dropped and not explicit_replace:
        return em_http.error(
            "would_drop_keys",
            f"This body would delete {len(dropped)} existing setting(s): "
            f"{', '.join(dropped)}. Config POSTs replace rather than merge — "
            f"send the full config (read-modify-write), or pass replace=true "
            f"if the deletion is intended.",
            409,
        )

    await loop.run_in_executor(None, db.set_global_device_config, config)

    # Push every connected device its own resolved effective config.
    pushed = []
    for device_id, live in list(em_state.devices.items()):
        effective = await loop.run_in_executor(
            None, db.get_effective_device_config, device_id
        )
        await em_device_config.apply_live_config(device_id, live, effective)
        pushed.append(device_id)

    if pushed:
        log.info(f"[api] Global config pushed to {len(pushed)} device(s): {pushed}")

    if (config.get("controllerEndpoints") or []) != endpoints_before:
        # Connected devices get the file now; the rest on their next connect,
        # which must not be skipped by a reconcile stamp from before the change.
        em_reconcile.stamps.clear()
        for device_id, live in list(em_state.devices.items()):
            em_tasks.spawn(em_payloads.push_endpoints(live, device_id))

    # Reconcile BT proxies for every approved device — offline ones included
    # (proxy mDNS/port lifecycle is independent of the device connection,
    # unlike the config push above). No longer filtered on inheritance: a
    # device overriding some other section still tracks the fleet's
    # bleProxyEnabled, and reconcile is idempotent either way.
    all_rows = await loop.run_in_executor(None, db.get_all_devices)
    for row in all_rows:
        if row["approved"]:
            await em_ble_proxy.reconcile(row["device_id"])

    return em_http.ok({"config": config, "pushed_to": pushed})
