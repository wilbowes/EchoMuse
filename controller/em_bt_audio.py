"""
em_bt_audio.py — Bluetooth speaker output, the controller's half
================================================================

The Echo streams its music plane to a paired Bluetooth speaker itself
(device/internal/bluetooth, A2DP over our own BR/EDR stack); voice replies
stay on the Echo. The controller only manages it:

  - `btAudioEnabled` and `btSpeaker` are ordinary config keys, in their own
    section because the speaker is per Echo.
  - Discovery, pairing and forgetting are requests: the dashboard POSTs, the
    controller sends `bt_audio` to the device with a request id, and waits for
    the `bt_audio_result` that echoes it. Discovery and pairing take seconds.
  - Every result and every state change carries the stream status and the
    paired list; the latest is kept here for the device view. The stats tick
    carries the status too (`btAudio`), so a missed message heals in 30s.

Speaker buttons (AVRCP) arrive as action "button" and are logged; mapping
them to Home Assistant's media player is later work.

Gated on the `bt_audio` capability, never on a version.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

log = logging.getLogger("echomuse.btaudio")

CAPABILITY = "bt_audio"
ACTIONS = ("discover", "pair", "forget", "status")

# How long each request may take on the device, before the controller gives
# up: discovery is the inquiry plus name requests, pairing a page (10.24s),
# the user's confirmation window on the speaker, and encryption.
_SLACK_S = 10.0
_TIMEOUT_S = {"discover": 45.0, "pair": 75.0, "forget": 15.0, "status": 10.0}

_state: dict[str, dict] = {}            # device_id -> {"status", "paired"}
_waiters: dict[str, asyncio.Future] = {}  # request id -> future


def capable(live) -> bool:
    return CAPABILITY in (getattr(live, "capabilities", None) or [])


def state(device_id: str) -> dict | None:
    """The last status and paired list the device reported, or None."""
    s = _state.get(device_id)
    return dict(s) if s else None


def note_stats(device_id: str, bt_status) -> None:
    """The stats tick's btAudio: refreshes the status, keeps the paired list."""
    if isinstance(bt_status, dict):
        _state.setdefault(device_id, {})["status"] = bt_status


def forget_device(device_id: str) -> None:
    _state.pop(device_id, None)


async def on_result(device, msg: dict) -> None:
    """A bt_audio_result from the device: a reply, a state change, or a button."""
    device_id = device.device_id
    if msg.get("action") == "button":
        log.info(f"[bt_audio] {device_id}: speaker button {msg.get('op')!r}")
        return
    entry = _state.setdefault(device_id, {})
    if isinstance(msg.get("status"), dict):
        entry["status"] = msg["status"]
    if isinstance(msg.get("paired"), list):
        entry["paired"] = msg["paired"]
    fut = _waiters.pop(str(msg.get("id") or ""), None)
    if fut is not None and not fut.done():
        fut.set_result(msg)


def validate(body: dict) -> tuple[dict | None, str | None]:
    """The request the dashboard sent, cleaned, or an error to return."""
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    action = body.get("action")
    if action not in ACTIONS:
        return None, f"action must be one of {', '.join(ACTIONS)}"
    req: dict = {"action": action}
    if action in ("pair", "forget"):
        addr = str(body.get("addr") or "").strip()
        if not _is_bdaddr(addr):
            return None, "addr must be a Bluetooth address, AA:BB:CC:DD:EE:FF"
        req["addr"] = addr.upper()
    if action == "pair" and body.get("name"):
        req["name"] = str(body["name"])[:64]
    if action == "discover":
        try:
            secs = int(body.get("seconds") or 10)
        except (TypeError, ValueError):
            return None, "seconds must be a number"
        req["seconds"] = max(2, min(secs, 30))
    return req, None


def _is_bdaddr(s: str) -> bool:
    parts = s.split(":")
    return len(parts) == 6 and all(
        len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts)


async def request(live, req: dict) -> dict:
    """Send one bt_audio request and wait for its result. Raises
    asyncio.TimeoutError when the device does not answer."""
    rid = uuid.uuid4().hex
    fut = asyncio.get_running_loop().create_future()
    _waiters[rid] = fut
    try:
        await live.send_control({"type": "bt_audio", "id": rid, **req})
        return await asyncio.wait_for(fut, timeout=_TIMEOUT_S[req["action"]] + _SLACK_S)
    finally:
        _waiters.pop(rid, None)
