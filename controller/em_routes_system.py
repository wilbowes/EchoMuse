"""
System routes: the controller's status (version, uptime, link ports, loop
lag, the dashboard bundle stamp), controller-wide settings, and the support
bundle.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

import em_auth as auth
import em_db as db
import em_http
import em_ingressauth
import em_player
import em_process_stats
import em_releases
import em_routes_static
import em_state
import em_support
from version import VERSION as CONTROLLER_VERSION
from version import firmware_update as _firmware_update

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    app.router.add_get("/api/support/bundle",  _get_support_bundle)
    app.router.add_get("/api/system/status",    _get_system_status)
    app.router.add_get("/api/system/config",    _get_system_config)
    app.router.add_patch("/api/system/config",  _patch_system_config)


# Per-device log lines in a support bundle, after thinning. Deep enough that
# a startup line survives a day of chatter, small enough that six devices do
# not bury the controller's own log.
DEVICE_LOG_LINES = 120


@auth.require_auth
async def _get_system_status(request: web.Request) -> web.Response:
    """GET /api/system/status"""
    loop = asyncio.get_event_loop()
    all_rows = await loop.run_in_executor(None, db.get_all_devices)
    fleet_base_os, approval_mode = await loop.run_in_executor(None, lambda: (
        db.fleet_base_os(), db.get_config("device_approval", "strict")))
    release = await em_releases.cached_firmware()

    return em_http.ok({
        "controller_version": CONTROLLER_VERSION,
        # This controller's device address and ports — what a new entry in
        # the controller address list starts from.
        "link": dict(em_state.link),
        # The mtime stamped onto the dashboard bundle's URL by
        # _serve_dashboard, so a running page can tell whether the JavaScript
        # it is executing is still the JavaScript this controller serves.
        #
        # A long-lived SPA tab survives an add-on update and keeps its old
        # bundle indefinitely — the URL is cache-busted, but only on a page
        # LOAD. Nothing in the page could notice, and `controller_version`
        # above made it worse rather than better: it is read from the server,
        # so it reports the NEW version while the page runs the OLD code.
        # Measured 2026-09-10, when a wizard run on a stale tab silently
        # skipped a provisioning step that had shipped hours earlier and the
        # header cheerfully named a version whose code was not running.
        #
        # Compared rather than displayed, so it does not matter that an mtime
        # is meaningless to a person; `version.py` cannot be used here because
        # a local build is "dev" for every build and would never differ.
        "bundle_version": em_routes_static.bundle_version(),
        # True when running as a Home Assistant add-on behind Supervisor's
        # ingress proxy. Presentation only — the dashboard is the same
        # dashboard either way, with the same features, and nothing should
        # be gated on this. It exists so the SPA can stop drawing chrome
        # Home Assistant already draws (its own panel header and title) and
        # can avoid offering a theme toggle that fights HA's theme.
        "ha_ingress": em_ingressauth.INGRESS_ONLY,
        # Which userspaces the fleet has reported (schema v21). Sorted for a
        # stable response; EMPTY means nothing has ever said, which is not the
        # same as "all Android" and must not be read that way — a control
        # disabled on the strength of not knowing is worse than one that is
        # merely useless on this fleet.
        "fleet_base_os": sorted(fleet_base_os),
        # Peak asyncio event-loop stall since start (ms). Non-trivial values
        # mean the controller itself delayed speaker frames and LED updates.
        "loop_lag_peak_ms": round(em_state.loop_lag_peak_ms, 1),
        "connected":      len(em_state.devices),
        "total_devices":  len(all_rows),
        "pending":        sum(1 for r in all_rows if not r["approved"]),
        "approval_mode":  approval_mode,
        # Whether the BACKGROUND poll runs (#159). With it off, "No release
        # info" and a GitHub outage are indistinguishable from the Updates
        # tab, and the tab is where someone goes to find out — so the reason
        # for the blank has to be visible there or it reads as a fault and
        # sends them debugging their network.
        #
        # Deliberately not "are updates available": "Check now" still works
        # when this is False, because a button press is a request rather than
        # background traffic. The flag says the poll is off, nothing more.
        "update_checks_enabled": em_releases.check_interval() > 0,
        "latest_release": release["version"] if release else None,
        # Controller update, surfaced alongside the firmware one so the header
        # can badge it without a second round trip. Read-only by design: the
        # controller runs as a container the user owns, and updating it is a
        # `docker compose pull` they perform — there is deliberately no action
        # here, only the information needed to decide to take it.
        "controller_update": em_releases.controller_update(),
        "updates_available": sum(
            1 for r in all_rows
            if release and _firmware_update(r["firmware_ver"], release["version"])
        ),
    })


@auth.require_admin
async def _get_system_config(request: web.Request) -> web.Response:
    """GET /api/system/config — full system_config table."""
    loop = asyncio.get_event_loop()
    config = await loop.run_in_executor(None, db.get_all_config)
    # Don't expose schema_version — internal detail
    config.pop("schema_version", None)
    return em_http.ok(config)


@auth.require_admin
async def _patch_system_config(request: web.Request) -> web.Response:
    """
    PATCH /api/system/config

    Body: {key: value, ...}
    Only known, mutable keys are accepted.
    """
    MUTABLE_KEYS = {
        "device_approval",
        "session_expiry_days",
        "update_check_interval",
        "github_repo",
    }
    body = await em_http.json_body(request)
    loop = asyncio.get_event_loop()

    updated = {}
    unknown = []
    for key, value in body.items():
        if key not in MUTABLE_KEYS:
            unknown.append(key)
            continue
        await loop.run_in_executor(None, db.set_config, key, str(value))
        updated[key] = value

    if unknown:
        return em_http.error(
            "unknown_config_key",
            f"Unknown or immutable config key(s): {', '.join(unknown)}",
            400,
        )
    return em_http.ok(updated)


@auth.require_admin
async def _get_support_bundle(request: web.Request) -> web.Response:
    """
    GET /api/support/bundle — one file to attach to an issue.

    Admin-only, and deliberately a download rather than a display: it is
    meant to be reviewed before it is shared. The privacy contract lives in
    em_support (allowlist, no speech, no labels, no network identifiers) and
    is enforced there, not here — this function only gathers.
    """
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, db.get_all_devices)

    since = time.time() - 24 * 3600
    turns, metrics, counters = [], [], []
    device_configs, live_state, logs = {}, {}, []

    health = await loop.run_in_executor(None, db.latest_health)
    for row in rows:
        did = row["device_id"]
        device_configs[did] = await loop.run_in_executor(
            None, db.get_effective_device_config, did)
        live = em_state.devices.get(did)
        live_state[did] = {
            "connected":    live is not None,
            # Capabilities decide which HA entities are advertised at all,
            # which is the first thing to check when one is "missing".
            "capabilities": list(getattr(live, "capabilities", []) or []) if live else [],
            # When ambient_light is missing from the list above, this says
            # WHY: no_chip (hardware revision without the part), no_attribute
            # (driver has not bound), or ok. None means firmware too old to
            # report it — not a fault. Carries the i2c device names it saw,
            # which is what makes a no_chip answer checkable rather than
            # merely asserted, and identifies an unfamiliar board revision
            # the first time one turns up (#90).
            "ambient_light_status": getattr(live, "ambient_light_status", None) if live else None,
            "muted":        getattr(live, "muted", None) if live else None,
            "rtt_ms":       getattr(live, "rtt_last_ms", None) if live else None,
            "volume":       getattr(live, "volume", None) if live else None,
            "media_state":  em_player.state(did),
            "stats":        em_support.redact_stats(live.stats if live else None),
            # Latest boot's reason and eMMC wear (schema v28): hardware
            # facts, nothing about the owner.
            "boot":         em_support.redact_boot(health.get(did)),
        }
        turns += await loop.run_in_executor(None, db.get_turns, did, 50, since)
        # get_device_metrics resolves its own rows and does NOT carry the
        # device, so without this every device's hours pooled into one
        # anonymous list — six devices' CPU and memory with no way to tell
        # whose was whose. (`_pick` wants .keys(), which a dict already has.)
        metrics += [dict(m, device_id=did)
                    for m in await loop.run_in_executor(None, db.get_device_metrics, did, since)]
        counters += await loop.run_in_executor(None, db.get_wake_counters, did, since)
        # Fetch deep and thin, rather than fetching 100 and shipping noise:
        # 89% of this table is [mem] heap dumps, so a flat 100 was ~11 lines
        # of evidence. Newest first, which is what thin_noise expects.
        raw = await loop.run_in_executor(None, db.get_device_logs, did, 500, None)
        pairs = [(lg["ts"], f"{lg['ts']} [{lg['level']}] {lg['source']}: {lg['message']}")
                 for lg in raw]
        # Sorted oldest-first at the end: a log someone reads should run
        # forwards, and per-device blocks in reverse order do not.
        logs += em_support.thin_noise(pairs, key=lambda p: p[1])[:DEVICE_LOG_LINES]

    bundle = em_support.build(
        controller_version=CONTROLLER_VERSION,
        devices=rows,
        fleet_config=await loop.run_in_executor(None, db.get_global_device_config),
        schema_version=len(db.MIGRATIONS),
        turns=turns,
        metrics=metrics,
        counters=counters,
        device_configs=device_configs,
        live_state=live_state,
        controller_log=em_process_stats.log_ring.tail(),
        device_log=[ln for _, ln in sorted(logs)],
        # From the user table, not guessed at: an account name in log prose
        # has nothing to pattern-match on. Mapped to the ROLE it is replaced
        # with — "an admin opened a shell" is the diagnostic content, and
        # this is a single-operator system, so a positional alias would be a
        # one-to-one stand-in for a real person.
        accounts={u["username"]: u["role"] for u in
                  await loop.run_in_executor(None, db.get_all_users)},
        controller_stats=await loop.run_in_executor(None, em_process_stats.controller_stats),
    )
    body = em_support.to_json(bundle)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return web.Response(
        body=body.encode(),
        content_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="echomuse-support-{stamp}.json"'},
    )
