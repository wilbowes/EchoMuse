"""
Sending one payload to every connected dashboard.

Split out of what is now `em_events.push` so it can be tested without
aiohttp. The loop used to walk the live set of clients while awaiting each
send, and a dashboard connecting or disconnecting during that await changed
the set under it: `RuntimeError: Set changed size during iteration`, raised into
whichever caller was broadcasting — often a device's control handler, which
then dropped the message it was processing.
"""

from __future__ import annotations


async def broadcast(clients: set, payload: str) -> None:
    """
    Send `payload` to each client; remove the ones whose send fails.

    Iterates a snapshot, so the set may change during any await. A client
    added mid-broadcast misses this event, which is fine: it is sent a full
    snapshot when it connects.
    """
    dead = []
    for ws in list(clients):
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
    clients.difference_update(dead)
