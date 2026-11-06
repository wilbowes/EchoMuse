"""
The Echoes themselves: the device list and one device, renaming,
approving and removing one, its config, WiFi and logs, and the two
secrets fetched from a device on request (the Sendspin pairing token and
the BLE proxy key).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from aiohttp import web

import em_auth as auth
import em_ble_proxy
import em_config_sections as sections_mod
import em_config_types
import em_db as db
import em_dbwriter
import em_device_config
import em_device_view
import em_esphome
import em_events
import em_http
import em_identify
import em_labels
import em_pairing
import em_reconcile
import em_shell
import em_state
import em_wifi

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    # Devices. A literal path wins over a parameterised one in any order;
    # order decides only between parameterised routes that overlap
    # (tests/test_route_resolution.py).
    app.router.add_get("/api/devices",                    _get_devices)
    app.router.add_get("/api/devices/pending",            _get_pending)
    app.router.add_get("/api/devices/{id}",               _get_device)
    app.router.add_patch("/api/devices/{id}",             _patch_device)
    app.router.add_delete("/api/devices/{id}",            _delete_device)
    app.router.add_post("/api/devices/{id}/approve",      _post_approve)
    app.router.add_post("/api/devices/{id}/identify",     _post_identify)
    app.router.add_get("/api/devices/{id}/config",        _get_device_config)
    app.router.add_get("/api/devices/{id}/sendspin/token", _get_sendspin_token)
    app.router.add_get("/api/devices/{id}/ble_proxy/key", _get_ble_proxy_key)
    app.router.add_post("/api/devices/{id}/config",       _post_device_config)
    app.router.add_get("/api/devices/{id}/logs",          _get_device_logs)
    app.router.add_post("/api/devices/{id}/wifi",         _post_device_wifi)
    app.router.add_post("/api/devices/{id}/wifi/scan",    _post_device_wifi_scan)


@auth.require_auth
async def _get_devices(request: web.Request) -> web.Response:
    """GET /api/devices — all devices, live state merged with DB."""
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_all_devices)
    health = await loop.run_in_executor(None, db.latest_health)
    return em_http.ok([em_device_view.merge_device(row, health.get(row["device_id"])) for row in rows])


@auth.require_auth
async def _get_pending(request: web.Request) -> web.Response:
    """GET /api/devices/pending — unapproved devices."""
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_pending_devices)
    return em_http.ok([em_device_view.merge_device(row) for row in rows])


@auth.require_auth
async def _get_device(request: web.Request) -> web.Response:
    """GET /api/devices/{id}"""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)
    return em_http.ok(em_device_view.merge_device(row))


@auth.require_admin
async def _patch_device(request: web.Request) -> web.Response:
    """PATCH /api/devices/{id} — update label."""
    device_id = request.match_info["id"]
    body  = await em_http.json_body(request)
    label = _require_label(body)
    await _require_unique_label(label, device_id)

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    await loop.run_in_executor(None, db.set_device_label, device_id, label)
    await em_events.push({"type": "device_update", "device_id": device_id,
                       "state": {"label": label}})
    # Music Assistant lists the Sendspin player by this name; the device
    # restarts the player to re-advertise it.
    live = em_state.devices.get(device_id)
    if live is not None and getattr(live, "sendspin_capable", False):
        with contextlib.suppress(Exception):
            await live.send_control({"type": "config", "sendspinName": label})
    return em_http.ok({"device_id": device_id, "label": label})


# How long to wait for a device to answer sendspin_token_request. It is a
# file read and one message on the control plane; seconds of slack are for
# this fleet's link stalls, not for the work.
SENDSPIN_TOKEN_TIMEOUT_S = 8


@auth.require_admin
async def _get_ble_proxy_key(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/ble_proxy/key — the encryption key Home Assistant
    asks for when it adds or re-authenticates this Echo's Bluetooth proxy.

    Handed to the one request and nothing else: never logged, never put in an
    event (events reach every open tab and support bundles). It is all that
    stands between the LAN and whatever the proxy can connect to. Admin only
    for the same reason. There is a key only while connections are on —
    a passive proxy is unencrypted and has nothing to show.
    """
    device_id = request.match_info["id"]
    status = em_ble_proxy.get_status(device_id)
    if status is None or not status.get("connections"):
        return em_http.error("connections_off",
                      "Turn on Bluetooth connections for this Echo first", 409)
    key = await asyncio.get_event_loop().run_in_executor(
        None, db.ensure_ble_proxy_key, device_id)
    if not key:
        return em_http.error("not_found", "Unknown device", 404)
    return em_http.ok({"key": key})


@auth.require_admin
async def _get_sendspin_token(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/sendspin/token — the pairing token to paste
    into Music Assistant.

    Asked of the device each time and never stored or logged: it carries the
    device's pairing key, and anyone holding it can pair with the device.
    Admin only for the same reason.
    """
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "The Echo is offline", 409)
    if not getattr(live, "sendspin_capable", False):
        return em_http.error("unsupported", "This Echo's firmware has no Sendspin player", 409)
    waiter = asyncio.get_event_loop().create_future()
    live.sendspin_token_waiter = waiter
    try:
        await live.send_control({"type": "sendspin_token_request"})
        reply = await asyncio.wait_for(waiter, SENDSPIN_TOKEN_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception):
        return em_http.error("timeout", "The Echo did not answer", 504)
    finally:
        if live.sendspin_token_waiter is waiter:
            live.sendspin_token_waiter = None
    if reply.get("error") or not reply.get("token"):
        return em_http.error("sendspin_off", "Turn Sendspin on for this Echo first", 409)
    return em_http.ok({"token": reply["token"], "clientId": reply.get("clientId")})


@auth.require_admin
async def _delete_device(request: web.Request) -> web.Response:
    """DELETE /api/devices/{id} — remove from registry."""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    await loop.run_in_executor(None, db.delete_device, device_id)
    # Row gone → reconcile tears down any BT proxy listener/mDNS for it.
    await em_ble_proxy.reconcile(device_id)
    # The satellite needs the same, and had no equivalent: it survives an
    # ordinary disconnect on purpose, so a delete used to leave it in
    # `_servers` holding the old port for a re-added device to inherit
    # silently.
    await em_esphome.device_deleted(device_id)
    # Free the device's cached OWW models (#512), or a deleted device keeps its
    # models — and their ONNX sessions — for the life of the process.
    em_state.forget_oww_models(device_id)
    # A re-added device is the one whose payloads are least likely to be
    # right, so it must not inherit the deleted row's debounce and skip its
    # first reconcile — the bounce below has it redialling within seconds.
    em_reconcile.forget(device_id)
    # A refusal belongs to the deleted row; the device comes back as pending.
    em_device_view.clear_link_refused(device_id)
    em_pairing.forget(device_id)
    # ...and the device is told to redial, or it never notices it was deleted.
    # Link auth is decided once, at register time, so a connected device keeps
    # running on the socket it already has: it vanishes from the dashboard and
    # carries on serving turns, and only comes back as pending after something
    # else drops the link — a reboot, a controller restart, a WiFi blip. The
    # bounce is what makes delete mean "start over" within seconds instead of
    # whenever. Deliberately AFTER the row is gone: the device redials in 5s
    # and must find an empty registry, or it re-registers into the row we were
    # deleting. em_linkauth ignores the token it still carries (rule 3), so it
    # arrives as pending — except under REQUIRE_DEVICE_TLS, where it is
    # refused and re-provisioning over USB is the intended path.
    await _disconnect_device(device_id)
    await em_events.push({"type": "device_deleted", "device_id": device_id})
    return em_http.ok({})


async def _disconnect_device(device_id: str) -> None:
    """
    Close a device's control plane, and any shell session riding on it.

    Only the control plane is closed: the device's own loop cancels its data
    client when control drops (`control.go` Run) and re-establishes both on
    the next dial, so closing `/data` here would only race that. The shell
    plane is separate and demand-opened, so an open session would otherwise
    hang against a device that is about to redial. `em_shell.release_ws` is
    called even with no programmatic session registered, because the
    `shell_close` it sends is the only thing that ends an INTERACTIVE
    dashboard session — those deliberately do not set `em_shell.sockets`.
    """
    live = em_state.devices.get(device_id)
    if live is None:
        return
    await em_shell.release_ws(device_id, live)
    try:
        await live.control_ws.close()
    except Exception as e:
        log.warning(f"[api] Could not close control plane for {device_id}: {e}")


@auth.require_admin
async def _post_approve(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/approve

    Body: {label, config?}
    Approves the device, assigns a label, and optionally overrides config.
    If the device is currently connected in pending state it will be
    accepted on its next retry (within 30s).
    """
    device_id = request.match_info["id"]
    body   = await em_http.json_body(request)
    label  = _require_label(body)
    await _require_unique_label(label, device_id)
    config = body.get("config")  # optional

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)
    if row["approved"]:
        return em_http.error("already_approved", "Device is already approved", 409)

    await loop.run_in_executor(None, db.approve_device, device_id, label, config)
    # Approval is the one human decision: it also issues link credentials when
    # the device next connects without them (em_pairing).
    em_pairing.approve(device_id)
    await em_events.push({"type": "device_approved", "device_id": device_id,
                       "label": label})
    return em_http.ok({"device_id": device_id, "label": label})


@auth.require_admin
async def _post_identify(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/identify

    Flashes this Echo's ring for em_identify.DURATION_S so a person can match
    the dashboard row to the device in front of them (#860). Refused, with the
    reason, when the ring is not ours to take: see em_identify.decide.
    """
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)
    verdict = em_identify.decide(
        connected=live is not None,
        led_anim_capable=bool(live and live.led_anim_capable),
        muted=bool(live and live.muted),
        turn_running=bool(live and live.voice_lock.locked()),
        speaking=bool(live and (live.speaking or live.speaker_busy)),
        alarm_ringing=bool(live and live.timer_alarm_ringing),
    )
    if verdict != em_identify.OK:
        return em_http.error(verdict, em_identify.MESSAGES[verdict],
                             em_identify.STATUS[verdict])
    await live.send_led_anim(dict(em_identify.IDENTIFY_ANIM))
    return em_http.ok({"device_id": device_id, "seconds": em_identify.DURATION_S})


@auth.require_auth
async def _get_device_config(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/config — effective config, scoping, and fleet view."""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)
    config = await loop.run_in_executor(None, db.get_effective_device_config, device_id)
    sections = await loop.run_in_executor(None, db.get_device_config_sections, device_id)
    return em_http.ok({
        "config":            config,
        "config_sections":   sections,
        # Compat view for older readers: no overridden sections == fleet.
        "use_global_config": not sections,
    })


@auth.require_admin
async def _post_device_config(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/config

    Body may include config_sections (list of section ids this device
    overrides), any config fields, and — for older clients —
    use_global_config (bool).

    Scoping is per section (see em_config_sections). Values supplied for a
    section the device does not override are ignored: the device follows the
    fleet there, and storing shadow values would silently resurrect them if
    the section were ever switched back.

    use_global_config is accepted as a compat alias: true == override
    nothing, false == override everything. That is exactly what the boolean
    meant before v8.

    If neither key is present the device's current scoping is left alone and
    only the in-scope values are updated.
    """
    device_id = request.match_info["id"]
    body = await em_http.json_body(request)

    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    sections_body     = body.pop("config_sections", None)
    use_global        = body.pop("use_global_config", None)
    explicit_replace  = bool(body.pop("replace", False))

    # Compat: map the old boolean onto the section model.
    if sections_body is None and use_global is not None:
        sections_body = [] if use_global else list(sections_mod.SECTION_IDS)

    if sections_body is None:
        new_sections = await loop.run_in_executor(
            None, db.get_device_config_sections, device_id
        )
    else:
        if not isinstance(sections_body, list):
            return em_http.error("bad_request", "config_sections must be a list", 400)
        unknown = [s for s in sections_body if s not in sections_mod.SECTIONS]
        if unknown:
            return em_http.error(
                "bad_request",
                f"Unknown config section(s): {', '.join(map(str, unknown))}. "
                f"Valid: {', '.join(sections_mod.SECTION_IDS)}.",
                400,
            )
        new_sections = sections_mod.normalise(sections_body)

    in_scope = sections_mod.keys_for(new_sections) | sections_mod.STATE_KEYS

    # Same replace-not-merge trap as the global endpoint (see em_device_config.dropped_keys),
    # but scoped: only keys that REMAIN in scope can be accidentally dropped.
    # Keys leaving scope are being deliberately handed back to the fleet, and
    # flagging those would make every legitimate un-override a 409.
    stored = await loop.run_in_executor(None, db.get_device_config, device_id)
    stored_in_scope = {k: v for k, v in stored.items() if k in in_scope}
    dropped = em_device_config.dropped_keys(body, stored_in_scope)
    if dropped and not explicit_replace:
        return em_http.error(
            "would_drop_keys",
            f"This body would delete {len(dropped)} existing setting(s): "
            f"{', '.join(dropped)}. Config POSTs replace rather than "
            f"merge — send the full config (read-modify-write), or pass "
            f"replace=true if the deletion is intended.",
            409,
        )

    # Validated BEFORE anything is written: a refusal must leave the stored
    # config untouched, not half-applied with the bad key rejected later.
    if (err := em_device_config.validate_console_timeout(body)):
        return em_http.error("bad_console_timeout", err, 400)
    # Judged against what the device has in force now, so re-sending a value
    # it already has is never refused (em_config_types.problems).
    before = await loop.run_in_executor(
        None, db.get_effective_device_config, device_id
    )
    if (bad := em_config_types.problems(
            {k: v for k, v in body.items() if k in in_scope}, before)):
        return em_http.error("bad_config_value", "; ".join(bad), 400)

    # Apply scoping first: set_device_config_sections prunes the values of
    # any section no longer overridden, so what follows writes into an
    # already-clean picture.
    if sections_body is not None:
        await loop.run_in_executor(
            None, db.set_device_config_sections, device_id, new_sections
        )
    values = {k: v for k, v in body.items() if k in in_scope}
    if values:
        current = await loop.run_in_executor(None, db.get_device_config, device_id)
        await loop.run_in_executor(
            None, db.set_device_config, device_id, {**current, **values}
        )

    config = await loop.run_in_executor(
        None, db.get_effective_device_config, device_id
    )

    # Push the EFFECTIVE config — with per-section scoping the body is
    # partial by design, so the device must be sent the resolved picture.
    pushed = False
    live = em_state.devices.get(device_id)
    if live is not None:
        await em_device_config.apply_live_config(device_id, live, config)
        log.info(f"[api] Config pushed to live device: {device_id}")
        pushed = True

    # BT proxy lifecycle follows bleProxyEnabled in the *effective* config —
    # reconcile unconditionally (idempotent): re-scoping a section changes the
    # effective value without the key appearing in the body.
    await em_ble_proxy.reconcile(device_id)

    await em_events.push({"type": "device_update", "device_id": device_id,
                       "state": {"config": config,
                                 "config_sections": new_sections,
                                 "use_global_config": not new_sections}})
    return em_http.ok({"device_id": device_id, "config": config,
                "config_sections": new_sections,
                "use_global_config": not new_sections, "pushed": pushed})


@auth.require_admin
async def _post_device_wifi(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/wifi — switch the device to a new WiFi network.

    Body: {"ssid": "...", "ssid_hex": "...", "psk": "..."} — ssid_hex is
    optional and names the exact SSID bytes from a scan; empty/absent psk =
    open network. Rules in em_wifi.

    Returns 202 immediately: the device owns the whole switch (associate →
    DHCP → reconnect gates, auto-rollback on any failure — see the device's
    internal/wifi package). The outcome arrives asynchronously as a
    wifi_result control message and is surfaced via the device_update
    event / the "wifi" field on the device object.
    """
    device_id = request.match_info["id"]
    body = await em_http.json_body(request)
    ssid = em_http.require_str(body, "ssid")
    ssid_hex = str(body.get("ssid_hex") or "")
    psk  = str(body.get("psk") or "")

    # Mirror the device's own validation so obvious mistakes fail fast
    # with a readable message instead of a full switch/rollback cycle.
    try:
        why = em_wifi.problem(em_wifi.ssid_bytes(ssid, ssid_hex), psk)
    except ValueError as e:
        why = str(e)
    if why:
        return em_http.error("invalid_credentials", why, 400)

    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)

    st = em_device_view.wifi_state(device_id)
    if st["pending"]:
        return em_http.error("wifi_change_in_progress",
                      f"A change to \"{st['pending']['ssid']}\" is already "
                      f"in progress", 409)

    st["pending"] = {"ssid": ssid, "started_at": time.time()}
    st["last_result"] = None
    # ssid_hex rides alongside the name. Firmware that predates it ignores
    # the field and uses the name, which is its behaviour today.
    change = {"type": "wifi_change", "ssid": ssid, "psk": psk}
    if ssid_hex:
        change["ssid_hex"] = ssid_hex
    await live.send_control(change)
    em_dbwriter.submit(db.log_device, device_id, "info", "controller",
                       f'WiFi change to "{ssid}" requested')
    await em_events.push({"type": "device_update", "device_id": device_id,
                       "state": {"wifi": st}})
    return em_http.ok({"device_id": device_id, "ssid": ssid, "status": "switching"},
               status=202)


@auth.require_admin
async def _post_device_wifi_scan(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/wifi/scan — ask the device for visible networks.

    Synchronous from the dashboard's point of view: sends wifi_scan and
    awaits the wifi_scan_result control message (the device's scan itself
    takes ~5s).
    """
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", "Device is not connected", 409)
    if getattr(live, "wifi_scan_future", None) is not None:
        return em_http.error("scan_in_progress", "A scan is already running", 409)

    fut = asyncio.get_event_loop().create_future()
    live.wifi_scan_future = fut
    try:
        await live.send_control({"type": "wifi_scan"})
        msg = await asyncio.wait_for(fut, timeout=20)
    except asyncio.TimeoutError:
        return em_http.error("scan_timeout",
                      "Device did not return scan results within 20s "
                      "(old firmware without WiFi support?)", 504)
    finally:
        live.wifi_scan_future = None
    if msg.get("error"):
        return em_http.error("scan_failed", msg["error"], 502)
    return em_http.ok({"networks": msg.get("networks") or []})


@auth.require_auth
async def _get_device_logs(request: web.Request) -> web.Response:
    """
    GET /api/devices/{id}/logs

    Query params:
      limit  — max rows (default 100, max 1000)
      before — cursor: return entries with ts < before (unix ms)
    """
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()

    row = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    try:
        limit = int(request.rel_url.query.get("limit", "100"))
    except ValueError:
        return em_http.error("invalid_param", "limit must be an integer", 400)

    before_param = request.rel_url.query.get("before")
    before_ts = None
    if before_param:
        try:
            before_ts = int(before_param)
        except ValueError:
            return em_http.error("invalid_param", "before must be a unix ms timestamp", 400)

    rows = await loop.run_in_executor(
        None, db.get_device_logs, device_id, limit, before_ts
    )
    entries = [
        {
            "id":        r["id"],
            "ts":        r["ts"],
            "level":     r["level"],
            "source":    r["source"],
            "message":   r["message"],
        }
        for r in rows
    ]
    return em_http.ok(entries)


def _require_label(body: dict) -> str:
    """The body's label, or a 400 naming the rule it broke (em_labels)."""
    label, err = em_labels.check_label(body.get("label"))
    if err:
        raise web.HTTPBadRequest(
            content_type="application/json",
            body=json.dumps({"error": err, "code": "invalid_label"}),
        )
    return label


async def _require_unique_label(label: str, device_id: str) -> None:
    """A 409 if another Echo already has this name (em_labels.duplicate_of).

    A hard refusal rather than a warning: two Echos with one name cannot be
    told apart in the dashboard, and Home Assistant quietly suffixes the
    second one's entity_ids with _2.
    """
    rows = await asyncio.get_event_loop().run_in_executor(None, db.get_all_devices)
    other = em_labels.duplicate_of(label, ((r["device_id"], r["label"]) for r in rows), device_id)
    if other is not None:
        raise web.HTTPConflict(
            content_type="application/json",
            body=json.dumps({"error": f'Another Echo is already called "{other}".',
                             "code": "duplicate_label"}),
        )
