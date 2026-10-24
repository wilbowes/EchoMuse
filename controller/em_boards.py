"""
Which Echo a device is, as Home Assistant shows it.

The firmware identifies its board (device/pkg/board) and reports the id on
registration as "board". Home Assistant reads the model once, from the
satellite's DeviceInfo and its mDNS TXT record, so the id is also stored, for
the satellites started before their Echo connects: one JSON map in
system_config under CONFIG_KEY, read at startup and written when a device's
board first arrives (no schema change for a fact that changes once a life).

Pure, so the suite imports it.
"""

import json

CONFIG_KEY = "device_boards"

# HA shows the part of project_name after the dot as the Model. Biscuit's
# string is the one every device has shown since the add-on existed; it must
# not change, or every existing HA device shows a new model.
MODELS = {
    "biscuit": "Echo Dot Gen 2 (biscuit)",
    "radar": "Echo Gen 2 (radar)",
    "donut": "Echo Dot Gen 3 (donut)",
}

# No board reported is firmware from before boards (biscuit only), and
# "unknown" is a device nothing identified, which runs biscuit's layout.
DEFAULT_MODEL = MODELS["biscuit"]


def known(board: str | None) -> bool:
    """Whether a reported id names a board, worth remembering."""
    return bool(board) and board != "unknown"


def model(board: str | None) -> str:
    """The model to show for a reported board id."""
    if not known(board):
        return DEFAULT_MODEL
    # A board newer than this controller still gets its own name rather than
    # biscuit's, which would be a wrong answer about the hardware.
    return MODELS.get(board, f"Echo ({board})")


def decode(raw: str | None) -> dict[str, str]:
    """The stored map. Anything unreadable reads as nothing known."""
    try:
        boards = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(boards, dict):
        return {}
    return {k: v for k, v in boards.items() if isinstance(k, str) and isinstance(v, str)}


def record(boards: dict[str, str], device_id: str, board: str | None) -> str | None:
    """
    Note a device's reported board in the live map. Returns the map to store
    when it changed, None when there is nothing to write.
    """
    if not known(board) or boards.get(device_id) == board:
        return None
    boards[device_id] = board
    return json.dumps(boards, sort_keys=True)
