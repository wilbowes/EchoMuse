"""
The Activity tab's routes: a device's recent voice turns and their saved
audio (admin only, transcripts redacted for everyone else), the wake word
samples captured for review, and the per-day activity aggregates.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from aiohttp import web

import em_auth as auth
import em_db as db
import em_http
import em_recordings
import em_wake_samples

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    app.router.add_get("/api/devices/{id}/turns",         _get_device_turns)
    app.router.add_get("/api/devices/{id}/activity",      _get_device_activity)
    app.router.add_get("/api/devices/{id}/turns/{turn}/audio", _get_turn_audio)
    app.router.add_get("/api/devices/{id}/wake-samples", _get_wake_samples)
    app.router.add_get("/api/devices/{id}/wake-samples/zip", _get_wake_sample_zip)
    app.router.add_patch("/api/devices/{id}/wake-samples/{sample}", _patch_wake_sample)
    app.router.add_delete("/api/devices/{id}/wake-samples/{sample}", _delete_wake_sample)
    app.router.add_get("/api/devices/{id}/wake-samples/{sample}/audio", _get_wake_sample_audio)


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


@auth.require_admin
async def _get_wake_sample_zip(request: web.Request) -> web.Response:
    """GET /api/devices/{id}/wake-samples/zip — every retained sample WAV for
    one device as a single archive, one directory per review label. Pruned
    files are skipped inside em_wake_samples.build_zip; an archive with
    nothing left is a 404, since retention makes "no files yet" ordinary."""
    device_id = request.match_info["id"]
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(
        None, db.get_wake_samples, device_id, em_wake_samples.KEEP_PER_DEVICE)
    data = await loop.run_in_executor(
        None, em_wake_samples.build_zip, device_id, rows)
    if data is None:
        return em_http.error("no_recording", "No saved audio for this device", 404)
    return web.Response(
        body=data,
        headers={
            "Content-Type":        "application/zip",
            "Content-Disposition": f'attachment; filename="wake-samples-{_slug(device_id)}.zip"',
        },
    )


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
