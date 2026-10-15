"""
em_api.py — EchoMuse Controller HTTP API + Dashboard
=====================================================

aiohttp web application running in the same asyncio event loop as the
WebSocket controller. Serves:

  /                         — dashboard SPA (static/index.html)
  /setup                    — first-run admin account creation
  /api/auth/*               — login, logout, current user
  /api/devices/*            — fleet management, config, logs, OTA
  /api/releases/*           — GitHub release tracking and deployment
  /api/system/*             — controller status and config
  WS /api/events            — live push: device state, logs, pending
  WS /api/devices/{id}/shell — proxied root shell on device

Path routing is handled by the existing websockets router in
em_controller.py — aiohttp handles /api/* and /, websockets handles
/control, /data, and /shell/{device_id}.

Usage (from em_controller.py main()):
    import em_api
    runner = await em_api.create_runner()
    await runner.setup()
    site = web.TCPSite(runner, host, port + 1)   # or same port via middleware
    await site.start()
    ...
    await runner.cleanup()

Live state shared with em_controller (the device registry, shell sessions)
is in em_state, which both import by name.
"""

import asyncio
import contextlib
import json
import logging
import posixpath as _posixpath
import re
from shlex import quote as _sh_quote
import time
from typing import Optional

import aiohttp
from aiohttp import web
import websockets

import em_db as db
import em_esphome
import em_events
import em_shell
import em_ota
import em_reconcile
import em_http
import em_state
import em_auth as auth
import em_ble_proxy
import em_config_sections as sections_mod
import em_config_types
import em_dbwriter
import em_device_config
import em_device_view
import em_tasks
import em_tcp
import em_labels
import em_firmware
import em_ingressauth
import em_routes_auth
import em_routes_config
import em_routes_link
import em_routes_oww
import em_routes_provision
import em_routes_shell
import em_routes_static
import em_routes_system
import em_routes_updates
import em_pairing
import em_recordings
import em_wake_samples
import em_wifi

log = logging.getLogger("echomuse.api")


# ─── Initialisation ───────────────────────────────────────────────────────────

async def create_app() -> web.Application:
    """
    Build and return the aiohttp Application.

    Routes are registered here. The app is not started — the caller
    creates an AppRunner and TCPSite.
    """
    # client_max_size defaults to 1 MB in aiohttp and the firmware is ~10.7 MB,
    # so /api/releases/upload rejects every real binary without this. Both
    # callers post there: the dashboard's Local Build panel and
    # controller/tools/ota.py.
    #
    # THIS IS A REGRESSION FROM AN AIOHTTP BUMP, NOT AN OLD BUG, and the
    # difference matters for what else to distrust. Measured across the two
    # pinned versions with a 3 MB multipart POST at a default Application:
    #
    #   aiohttp 3.13.5  -> 200   (streaming multipart bypassed the limit)
    #   aiohttp 3.14.3  -> 413   HTTPRequestEntityTooLarge
    #
    # 3.13.5 held from May until 2026-08-18, when #129's routine half moved to
    # 3.14.3 and broke local deploys silently — no CI job posts a real-sized
    # body at this endpoint, and the ordinary release path never touches it
    # (em_firmware fetches controller-side and pushes from there), so only a
    # developer deploying a local build ever met it.
    #
    # Set ABOVE the handler's limit on purpose: the handler's 413 names the
    # actual ceiling ("Binary exceeds 50 MB limit"), and it can only be the
    # error a user sees if the transport lets the body through first. A
    # transport limit equal to the application limit means the useful message
    # is unreachable by construction.
    app = web.Application(
        middlewares=[_ingress_only_middleware, _error_middleware],
        client_max_size=em_routes_updates.UPLOAD_MAX_BYTES + 8 * 1024 * 1024,
    )

    # Static / setup
    em_routes_static.register(app)

    em_routes_auth.register(app)

    # Devices. A literal path wins over a parameterised one in any order;
    # order decides only between parameterised routes that overlap
    # (tests/test_route_resolution.py).
    app.router.add_get("/api/devices",                    _get_devices)
    app.router.add_get("/api/devices/pending",            _get_pending)
    app.router.add_get("/api/devices/{id}",               _get_device)
    app.router.add_patch("/api/devices/{id}",             _patch_device)
    app.router.add_delete("/api/devices/{id}",            _delete_device)
    app.router.add_post("/api/devices/{id}/approve",      _post_approve)
    app.router.add_get("/api/devices/{id}/config",        _get_device_config)
    app.router.add_get("/api/devices/{id}/sendspin/token", _get_sendspin_token)
    app.router.add_get("/api/devices/{id}/ble_proxy/key", _get_ble_proxy_key)
    app.router.add_post("/api/devices/{id}/config",       _post_device_config)
    app.router.add_get("/api/devices/{id}/logs",          _get_device_logs)
    app.router.add_get("/api/devices/{id}/turns",         _get_device_turns)
    app.router.add_get("/api/devices/{id}/activity",      _get_device_activity)
    app.router.add_get("/api/devices/{id}/turns/{turn}/audio", _get_turn_audio)
    app.router.add_get("/api/devices/{id}/wake-samples", _get_wake_samples)
    app.router.add_patch("/api/devices/{id}/wake-samples/{sample}", _patch_wake_sample)
    app.router.add_delete("/api/devices/{id}/wake-samples/{sample}", _delete_wake_sample)
    app.router.add_get("/api/devices/{id}/wake-samples/{sample}/audio", _get_wake_sample_audio)
    app.router.add_post("/api/devices/{id}/wifi",         _post_device_wifi)
    app.router.add_post("/api/devices/{id}/wifi/scan",    _post_device_wifi_scan)

    em_routes_oww.register(app)
    em_routes_shell.register(app)

    # Updates: firmware, emOS, releases, the controller release notice
    em_routes_updates.register(app)

    # Global device config
    em_routes_config.register(app)

    # System
    em_routes_system.register(app)

    # Provisioning
    em_routes_provision.register(app)
    em_routes_link.register(app)

    # Live events WebSocket
    app.router.add_get("/api/events", _ws_events)

    return app


async def create_runner() -> web.AppRunner:
    """Convenience wrapper — create_app + AppRunner."""
    app = await create_app()
    return web.AppRunner(app)


# ─── Middleware ───────────────────────────────────────────────────────────────

@web.middleware
async def _ingress_only_middleware(request: web.Request, handler):
    """
    As a Home Assistant add-on, the dashboard/API must only be reachable
    through the authenticated ingress gateway — the add-on has no other
    auth in front of it on the LAN otherwise. No-op (INGRESS_ONLY unset)
    for every deployment that isn't the add-on.
    """
    if (em_ingressauth.INGRESS_ONLY
            and request.remote != em_ingressauth.INGRESS_GATEWAY_IP):
        log.warning("Rejected non-ingress request from %s", request.remote)
        raise web.HTTPForbidden(text="Home Assistant Ingress is required")
    return await handler(request)


@web.middleware
async def _error_middleware(request: web.Request, handler):
    """
    Catch unhandled exceptions and return a consistent error shape.

    AuthError from em_auth is also caught here so route handlers don't
    need to handle it explicitly.
    """
    try:
        return await handler(request)
    except auth.AuthError as e:
        return e.to_response()
    except web.HTTPException:
        raise  # let aiohttp handle its own HTTP exceptions normally
    except Exception as e:
        log.exception(f"Unhandled error in {request.method} {request.path}")
        return em_http.error("internal_error", "An internal error occurred", 500)


# ─── Devices ──────────────────────────────────────────────────────────────────


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


@auth.require_auth
async def _get_device_turns(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/turns — recent voice-turn traces, newest last.
    Served from the persistent turns table (survives controller and device
    restarts). Powers the Activity tab's observability panel.

    Query params: limit (default 50, max 1000), since (epoch seconds)."""
    device_id = request.match_info["id"]
    try:
        limit = min(int(request.query.get("limit", 50)), 1000)
        since = request.query.get("since")
        since = float(since) if since is not None else None
    except ValueError:
        return em_http.error("bad_request", "limit/since must be numeric", 400)
    loop  = asyncio.get_event_loop()
    turns = await loop.run_in_executor(
        None, lambda: db.get_turns(device_id, limit, since)
    )
    return em_http.ok(_redact_turns_for(turns, request["user"]))


def _redact_turns_for(turns: list, user: dict) -> list:
    """
    Remove transcripts for a non-admin session.

    A transcript is the content of what someone said in their home — the
    same class of data as the recording it came from, differing only in
    format. Stripped HERE rather than hidden in the dashboard, because the
    dashboard is not what protects it: /api/devices/{id}/turns is a plain
    GET with a session token, so a UI-only rule protects nothing from
    anyone who opens the network tab.

    The rest of the row — timings, scores, outcome — is what the Activity
    tab is for and stays visible, so read-only access keeps its diagnostic
    value.
    """
    if user.get("role") == "admin":
        return turns
    return [{k: v for k, v in t.items() if k != "stt_text"} for t in turns]


@auth.require_admin
async def _get_turn_audio(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/turns/{turn}/audio — the saved mic audio for
    one voice turn, as a downloadable WAV. ADMIN ONLY.

    This is recognisable speech recorded in someone's home — the most
    sensitive thing the controller stores, and the reason saveUtterances is
    off by default. Under the add-on every Home Assistant user in the
    household can reach the dashboard (Supervisor's ingress view sets
    requires_auth=False and panel_admin only hides the sidebar entry), so
    read-only is no longer a synonym for "someone the operator trusts with
    the recordings".

    Only turns captured while saveUtterances was on have one, and only the
    newest em_recordings.KEEP_PER_DEVICE per device survive — a turn row
    older than that window still carries the filename but the file is gone,
    so a 404 here is an ordinary outcome, not an error state.

    The filename is derived from (device, turn) rather than taken from the
    row: em_recordings.resolve then re-checks that the file belongs to the
    device in the URL, so a turn id from another device can't be used to
    reach its audio."""
    device_id = request.match_info["id"]
    try:
        turn_id = int(request.match_info["turn"])
    except ValueError:
        return em_http.error("bad_request", "turn must be an integer", 400)

    loop = asyncio.get_event_loop()
    row  = await loop.run_in_executor(None, db.get_device, device_id)
    if row is None:
        return em_http.error("device_not_found", f"No device: {device_id}", 404)

    name = em_recordings.filename(device_id, turn_id)
    path = em_recordings.resolve(device_id, name) if name else None
    if path is None:
        return em_http.error("no_recording",
                      "No saved audio for this turn", 404)

    label = _slug(row["label"] or device_id)
    return web.FileResponse(
        path,
        headers={
            "Content-Type":        "audio/wav",
            "Content-Disposition": f'attachment; filename="{label}-turn{turn_id}.wav"',
            # Recordings are immutable once written and their names are
            # unique per turn, but the retention window means a name can
            # stop resolving — so cache privately and briefly, never shared.
            "Cache-Control":       "private, max-age=60",
        },
    )


def _slug(text: str) -> str:
    """Lowercase ASCII slug, safe for a Content-Disposition filename."""
    out = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return out or "device"


@auth.require_admin
async def _get_wake_samples(request: web.Request) -> web.Response:
    device_id = request.match_info["id"]
    try:
        limit = min(max(int(request.query.get("limit", 50)), 1), 100)
    except ValueError:
        return em_http.error("bad_request", "limit must be an integer", 400)
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_wake_samples, device_id, limit)
    return em_http.ok([dict(row) for row in rows])


@auth.require_admin
async def _patch_wake_sample(request: web.Request) -> web.Response:
    device_id = request.match_info["id"]
    try:
        sample_id = int(request.match_info["sample"])
    except ValueError:
        return em_http.error("bad_request", "sample must be an integer", 400)
    body = await em_http.json_body(request)
    label = body.get("label")
    if label not in ("wake", "not_wake", "uncertain"):
        return em_http.error("bad_request", "label must be wake, not_wake, or uncertain", 400)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, db.set_wake_sample_label,
                                    device_id, sample_id, label)
    return em_http.ok({"id": sample_id, "label": label}) if ok else em_http.error(
        "sample_not_found", "No wake sample with that id", 404)


@auth.require_admin
async def _delete_wake_sample(request: web.Request) -> web.Response:
    device_id = request.match_info["id"]
    try:
        sample_id = int(request.match_info["sample"])
    except ValueError:
        return em_http.error("bad_request", "sample must be an integer", 400)
    loop = asyncio.get_event_loop()
    name = await loop.run_in_executor(None, db.delete_wake_sample,
                                      device_id, sample_id)
    if name is None:
        return em_http.error("sample_not_found", "No wake sample with that id", 404)
    return em_http.ok({"id": sample_id, "deleted": True})


@auth.require_admin
async def _get_wake_sample_audio(request: web.Request) -> web.Response:
    device_id = request.match_info["id"]
    try:
        sample_id = int(request.match_info["sample"])
    except ValueError:
        return em_http.error("bad_request", "sample must be an integer", 400)
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_wake_samples, device_id, 100)
    row = next((item for item in rows if item["id"] == sample_id), None)
    path = em_wake_samples.resolve(device_id, row["audio_file"]) if row else None
    if path is None:
        return em_http.error("no_recording", "No saved audio for this sample", 404)
    return web.FileResponse(path, headers={
        "Content-Type": "audio/wav",
        "Content-Disposition": f'attachment; filename="{_slug(device_id)}-wake{sample_id}.wav"',
        "Cache-Control": "private, max-age=60",
    })


@auth.require_auth
async def _get_device_activity(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/activity?days=7 — aggregated activity stats
    for trend review: per-day turn buckets (counts, outcomes, latency
    percentiles, wake scores, underruns), per-wake-model rollups, hourly
    near-miss counters, and hourly hardware metrics (CPU/RAM/storage/RSSI)."""
    device_id = request.match_info["id"]
    try:
        days = min(int(request.query.get("days", 7)), 180)
    except ValueError:
        return em_http.error("bad_request", "days must be an integer", 400)
    since = time.time() - days * 86400

    loop     = asyncio.get_event_loop()
    turns    = await loop.run_in_executor(
        None, lambda: db.get_turns(device_id, 50_000, since)
    )
    counters = await loop.run_in_executor(
        None, lambda: db.get_wake_counters(device_id, since)
    )
    metrics  = await loop.run_in_executor(
        None, lambda: db.get_device_metrics(device_id, since)
    )

    def pct(sorted_vals, p):
        if not sorted_vals:
            return None
        return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]

    # Per-day buckets (local time), oldest first.
    day_buckets: dict[str, list[dict]] = {}
    for t in turns:
        day = time.strftime("%Y-%m-%d", time.localtime(t["ts"]))
        day_buckets.setdefault(day, []).append(t)

    days_out = []
    for day in sorted(day_buckets):
        ts_list   = day_buckets[day]
        ok        = [t for t in ts_list if t["outcome"] == "ok"]
        totals    = sorted(t["total_ms"] for t in ok if (t["total_ms"] or 0) > 0)
        scores    = [t["wake_score"] for t in ts_list if t["wake_score"] is not None]
        underruns = sum(t["underruns"] or 0 for t in ts_list)
        outcomes: dict[str, int] = {}
        for t in ts_list:
            outcomes[t["outcome"] or "?"] = outcomes.get(t["outcome"] or "?", 0) + 1
        days_out.append({
            "date":           day,
            "turns":          len(ts_list),
            "ok":             len(ok),
            "outcomes":       outcomes,
            "total_ms_p50":   pct(totals, 0.50),
            "total_ms_p95":   pct(totals, 0.95),
            "wake_score_avg": round(sum(scores) / len(scores), 3) if scores else None,
            "wake_score_min": round(min(scores), 3) if scores else None,
            "underruns":      underruns,
        })

    # Per-wake-model rollup — supports A/B-ing custom OWW models.
    models: dict[str, dict] = {}
    for t in turns:
        if not t["wake_model"]:
            continue
        m = models.setdefault(
            t["wake_model"], {"turns": 0, "score_sum": 0.0, "score_min": None}
        )
        m["turns"] += 1
        if t["wake_score"] is not None:
            m["score_sum"] += t["wake_score"]
            m["score_min"] = (
                t["wake_score"] if m["score_min"] is None
                else min(m["score_min"], t["wake_score"])
            )
    models_out = {
        name: {
            "turns":     m["turns"],
            "score_avg": round(m["score_sum"] / m["turns"], 3) if m["turns"] else None,
            "score_min": m["score_min"],
        }
        for name, m in models.items()
    }

    # On-device shadow comparison (schema v13) — the verdict, computed here so
    # a reader is not left to derive it from raw columns.
    #
    # Denominator is turns where the device was KNOWN to be scoring
    # (dev_shadow=1); a NULL score on those is a genuine miss, whereas a NULL
    # anywhere else is absence of data and must not be counted either way.
    scoring    = [t for t in turns if (t["dev_shadow"] or 0) == 1]
    # A turn is only COMPARABLE if the device was scoring against a bar this
    # controller's wake would have cleared. During playback the controller drops
    # to bargeInThreshold, so a turn that fired at 0.055 was never something a
    # device scoring against 0.5 could have caught — counting those as misses
    # made the agreement figure pessimistic, which is how this was found.
    # A device that reports no threshold (older firmware) is also not comparable:
    # unknown, rather than guessed at.
    def _comparable(t) -> bool:
        dev_thr = t["dev_threshold"]
        if dev_thr is None:
            return False
        wake_thr = t["wake_threshold"]
        return wake_thr is not None and wake_thr >= dev_thr

    compared   = [t for t in scoring if _comparable(t)]
    incomparable = len(scoring) - len(compared)
    agreed     = [t for t in compared if t["dev_wake_score"] is not None]
    deltas     = sorted(t["dev_wake_delta_ms"] for t in agreed
                        if t["dev_wake_delta_ms"] is not None)
    dev_scores = [t["dev_wake_score"] for t in agreed]
    crossings  = sum(r["dev_crossings"] or 0 for r in counters)
    shadow_out = {
        "turns_scoring":  len(scoring),
        "turns_compared": len(compared),
        # Turns where the device was scoring but the comparison is not valid —
        # the controller used a lower (barge-in) bar, or the device's threshold
        # is unknown. Reported rather than hidden: a large number here means the
        # agreement figure is describing a small slice of reality.
        "not_comparable": incomparable,
        "agreed":         len(agreed),
        "missed":         len(compared) - len(agreed),
        "agreement_pct":  round(100.0 * len(agreed) / len(compared), 1) if compared else None,
        # Signed: negative means the device crossed FIRST, which is the
        # expected direction — it scores the frame it just captured while the
        # controller scores the same frame after a network hop.
        "delta_ms_p50":   pct(deltas, 0.50),
        "delta_ms_p95":   pct(deltas, 0.95),
        "dev_score_avg":  round(sum(dev_scores) / len(dev_scores), 3) if dev_scores else None,
        "dev_score_min":  round(min(dev_scores), 3) if dev_scores else None,
        "crossings":      crossings,
        # Crossings that never matched a turn. This is the false-accept side of
        # the comparison, which per-turn rows structurally cannot show — but it
        # is an ESTIMATE, not a count: the hourly counters and the turn rows are
        # pruned on different schedules (WAKE_COUNTER_RETENTION_DAYS vs
        # TURN_RETENTION rows), so over a long window this drifts. Treat a
        # small number as noise and a large one as worth investigating.
        "unmatched_crossings": max(0, crossings - len(agreed)),
        "frames":         sum(r["dev_frames"] or 0 for r in counters),
        # Nonzero drops mean the device could not keep up, so every figure
        # above is describing a subset of the audio.
        "drops":          sum(r["dev_drops"] or 0 for r in counters),
    }

    return em_http.ok({
        "days":          days_out,
        "wake_models":   models_out,
        "wake_counters": [dict(r) for r in counters],
        "metrics":       metrics,
        "shadow":        shadow_out,
    })


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


# ─── Live events WebSocket ────────────────────────────────────────────────────

async def _ws_events(request: web.Request) -> web.WebSocketResponse:
    """
    WS /api/events

    Readonly access required. Dashboard connects once on load.
    Controller pushes device state changes, logs, and pending alerts
    in real time — no polling needed.
    """
    user = await auth.ws_resolve_session(request)
    if user is None:
        raise web.HTTPUnauthorized()

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    em_events.clients.add(ws)
    log.debug(f"[api] Events client connected ({user['username']}) "
              f"— {len(em_events.clients)} total")

    try:
        # Send full device snapshot on connect so the dashboard has
        # immediate state without waiting for the first push event.
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(None, db.get_all_devices)
        await ws.send_str(json.dumps({
            "type":    "snapshot",
            "devices": [em_device_view.merge_device(r) for r in rows],
        }))

        async for msg in ws:
            # Client shouldn't send anything, but handle gracefully
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

    finally:
        em_events.clients.discard(ws)
        log.debug(f"[api] Events client disconnected — "
                  f"{len(em_events.clients)} remaining")

    return ws


# ─── Periodic background tasks ────────────────────────────────────────────────

async def session_prune_loop() -> None:
    """Prune expired sessions hourly."""
    while True:
        await asyncio.sleep(3600)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, db.prune_sessions)
        except Exception as e:
            log.error(f"[api] Session prune error: {e}")


# ─── Helpers shared across em_controller ─────────────────────────────────────

# Supervisor decisions kept on the device, surviving the reboot that /tmp does
# not. Must match SUP_LOG in device_payloads/start_server.sh.
SUPERVISOR_LOG = "/data/local/etc/echomuse/supervisor.log"


async def _collect_supervisor_log(device_id: str) -> None:
    """
    Pull the device's supervisor log after a failed update, on reconnect.

    A failed update destroys its own evidence: everything start_server.sh
    logs goes to /tmp, which is RAM-backed, so the power cycle used to
    recover wipes exactly the lines that would explain it (2026-08-01, a
    device that never came back and could not be diagnosed afterwards).

    The persistent log fixes the storage half. This is the other half: the
    controller notices it is owed an explanation and fetches it the moment
    the device is reachable again, pushing it into that device's log events —
    so the evidence arrives where someone would look, instead of sitting in a
    file nobody knows to read.
    """
    live = em_state.devices.get(device_id)
    if live is None:
        return
    out = await em_shell.run(live, f"busybox tail -c 4096 {SUPERVISOR_LOG}", timeout=30.0)
    text = (out or "").strip()
    if not text:
        await em_events.push_log(device_id, "warn", "controller",
            "Update failed, and the device has no supervisor log — firmware "
            "predating it, or start_server.sh has not been synced yet "
            "(it takes effect on the next device reboot).")
        return
    await em_events.push_log(device_id, "warn", "controller",
        "Supervisor log from the failed update:\n" + text)


async def notify_device_connected(device_id: str, version: str | None = None) -> None:
    """
    Called by em_controller when a device successfully registers.

    Includes firmware_ver in the event so the dashboard's device cache is
    updated immediately on reconnect — prevents a stale-cache false-positive
    where the frontend sees the old version during an OTA reconnect window
    and incorrectly shows an auto-rollback warning.

    Pass version directly from the device handshake (preferred — no DB round-trip).
    If omitted, falls back to a DB lookup; assumes em_controller has already
    written the new firmware_ver before calling this.
    """
    event: dict = {"type": "device_connected", "device_id": device_id}
    if version is not None:
        event["firmware_ver"] = version
    else:
        loop = asyncio.get_event_loop()
        row = await loop.run_in_executor(None, db.get_device, device_id)
        if row:
            event["firmware_ver"] = row["firmware_ver"]
    await em_events.push(event)

    # Owed an explanation from a failed update? Collect it now the device is
    # reachable again. Removed from the set on the way in, so a flapping
    # device cannot queue repeated fetches, and scheduled rather than awaited
    # so a slow shell never delays the connect path.
    if device_id in em_ota.supervisor_log_wanted:
        em_ota.supervisor_log_wanted.discard(device_id)

        async def _collect_soon(_id=device_id):
            # The device has just registered; give its shell plane a moment
            # before demanding a session on it.
            await asyncio.sleep(3.0)
            try:
                await _collect_supervisor_log(_id)
            except Exception as e:
                log.warning(f"[api] supervisor log fetch failed for {_id}: {e}")

        em_tasks.spawn(_collect_soon())


async def notify_device_disconnected(device_id: str) -> None:
    """Called by em_controller when a device disconnects."""
    await em_events.push({"type": "device_disconnected", "device_id": device_id})


async def notify_pair_request(device_id: str, via: str) -> None:
    """A device asked to pair; the dashboard offers Approve pairing."""
    if em_pairing.request(device_id, via):
        log.info(f"[api] {device_id} asked to pair ({via})")
        await em_events.push_log(device_id, "info", "controller",
                              "Asked to pair — approve it in the dashboard")
        await em_events.push({"type": "device_pair_request", "device_id": device_id})


async def notify_device_pending(device_id: str, ip: str) -> None:
    """Called by em_controller when an unapproved device attempts connection."""
    await em_events.push({
        "type":      "device_pending",
        "device_id": device_id,
        "ip":        ip,
    })


# ─── Label checks ─────────────────────────────────────────────────────────────

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
