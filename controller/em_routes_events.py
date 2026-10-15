"""
The dashboard's live events WebSocket: device state, logs and update
progress pushed to every open tab.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
from aiohttp import web

import em_auth as auth
import em_db as db
import em_device_view
import em_events

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    app.router.add_get("/api/events", _ws_events)


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
