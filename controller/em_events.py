"""
Live events to every open dashboard: device state changes, log lines,
pending alerts. The dashboard connects once to /api/events (em_api's
_ws_events, which adds and removes its socket here) and is pushed each event
as it happens rather than polling.

Its own module because the device control handlers, the ESPHome satellite
and the API routes all push, and none of them should need em_api to do it.
"""

from __future__ import annotations

import json
import time

import em_broadcast
import em_db as db
import em_dbwriter

# Connected /api/events WebSocket clients.
clients: set = set()


async def push(event: dict) -> None:
    """
    Broadcast a JSON event to all connected /api/events clients.

    Called by route handlers and background tasks whenever device
    state changes.
    """
    if not clients:
        return
    await em_broadcast.broadcast(clients, json.dumps(event))


async def push_log(
    device_id: str,
    level: str,
    source: str,
    message: str,
) -> None:
    """
    Persist a controller-generated log entry and push it to event clients.

    The write is queued (em_dbwriter), not awaited: this is called inline from
    each device's control handler for every relayed log line, and awaiting the
    write held that device's pongs and wake events behind it.
    """
    em_dbwriter.submit(db.log_device, device_id, level, source, message)
    await push({
        "type":      "device_log",
        "device_id": device_id,
        "entry": {
            "ts":      int(time.time() * 1000),
            "level":   level,
            "source":  source,
            "message": message,
        },
    })
