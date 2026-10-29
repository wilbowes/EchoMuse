"""
Ask an Echo for its hardware and network snapshot (device/internal/diag).

A support bundle used to describe an Echo only from the controller's side, so
the mixer, ALSA's view of the sound card, the board's idme fields, the audio
drivers' kernel lines and the supplicant's state were asked for by hand in
issue after issue. Firmware announcing `diag_snapshot` answers a
`diag_request` with them; the reply is raw and is redacted by
em_support.redact_snapshot before it reaches a bundle.

Requested only while a bundle is built, never on a timer: it costs the Echo a
kernel log read and a wpa_cli spawn.
"""

from __future__ import annotations

import asyncio
import secrets

CAPABILITY = "diag_snapshot"
TIMEOUT_S = 6.0

# request id → (device id, future). The device id is checked on arrival so
# one Echo cannot answer for another.
_waiting: dict[str, tuple[str, asyncio.Future]] = {}


async def on_snapshot(device, msg: dict) -> None:
    """CONTROL_HANDLERS entry for `diag_snapshot`."""
    entry = _waiting.get(msg.get("id"))
    if entry is None:
        return
    device_id, fut = entry
    if device_id == device.device_id and not fut.done():
        fut.set_result(msg.get("snapshot"))


async def request(live, timeout: float = TIMEOUT_S) -> dict:
    """{"status": ok | unsupported | timeout | offline, "snapshot": ...}."""
    if live is None:
        return {"status": "offline"}
    if CAPABILITY not in (getattr(live, "capabilities", None) or ()):
        return {"status": "unsupported"}
    rid = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    _waiting[rid] = (live.device_id, fut)
    try:
        await live.send_control({"type": "diag_request", "id": rid})
        snap = await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return {"status": "timeout"}
    finally:
        _waiting.pop(rid, None)
    return {"status": "ok", "snapshot": snap}
