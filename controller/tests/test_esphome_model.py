"""
The ESPHome device model reported to Home Assistant follows the device's
own board, not a constant.

Two boards run this binary — `biscuit` (Echo Dot Gen 2) and `radar`
(Echo 2). HA's device page shows the `model` field under "Device info"
alongside the manufacturer, so a hardcoded "Echo Dot Gen 2 (biscuit)"
makes every Radar look like a Dot 2, with no signal anything is wrong.

The lookup is in ESPHOME_BOARD_MODELS; DeviceInfoRequest resolves it from
the owning server's `board_id`, which `device_connected()` sets before
the TCP listener starts, so HA's first request after a wizard setup
already sees the right value. Older firmware that doesn't report `board`
falls back to the biscuit default (None → biscuit), which is the
existing fleet's behaviour and the safer of the two: a mis-identifying
default that looks like a known board is better than an unknown one
that HA may treat as a new device.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from esphome.vendor import api_pb2

import em_esphome

CONTROLLER = Path(__file__).resolve().parents[1]
ESPHOME_SRC = (CONTROLLER / "em_esphome.py").read_text()


def _strip_py_comments(src: str) -> str:
    """Comments AND docstrings. A guard that strips only `#` lines matches
    the prose explaining the rule it is enforcing and passes whatever the
    code does."""
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    return "\n".join(re.sub(r"#.*$", "", line) for line in src.splitlines())


ESPHOME_CODE = _strip_py_comments(ESPHOME_SRC)


# ── The lookup ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("board_id,expected", [
    (None,    "Echo Dot Gen 2 (biscuit)"),  # older firmware without `board`
    ("biscuit", "Echo Dot Gen 2 (biscuit)"),
    ("radar",   "Echo 2 (radar)"),
])
def test_the_lookup_returns_the_right_model_for_each_known_board(
        board_id, expected):
    assert em_esphome.esphome_device_model(board_id) == expected


def test_an_unknown_board_falls_back_to_biscuit_not_to_an_empty_string():
    """
    A new board silently mis-identifying as biscuit is better than HA
    seeing an empty/unknown model — the latter triggers a new device
    entry on re-discovery. The fix is to extend ESPHOME_BOARD_MODELS.
    """
    assert em_esphome.esphome_device_model("not-a-real-board") == \
        em_esphome.esphome_device_model(None)


def test_a_missing_board_key_is_handled_explicitly():
    """
    The None key must be in the table. Without it, a get on None raises
    KeyError; the safer default lives in the table so older firmware (no
    `board` in the register message) keeps reporting the right thing.
    """
    assert None in em_esphome.ESPHOME_BOARD_MODELS
    # The fallback IS the biscuit string — same string the project shipped
    # as a constant before this change. Pinned so a re-order of the table
    # cannot move the existing fleet's display.
    assert em_esphome.esphome_device_model(None) == \
        em_esphome.ESPHOME_BOARD_MODELS["biscuit"]


# ── The static contract ──────────────────────────────────────────────────────


def test_device_info_request_resolves_the_model_from_the_owning_server():
    """
    A constant reference inside DeviceInfoResponse is the bug — the lookup
    has to read `self._owning_server.board_id`, otherwise the helper exists
    and nothing uses it.
    """
    # The DeviceInfoResponse block spans multiple lines (the helper call
    # sits above the response, the model + project_name fields sit inside
    # it). Slice from "DeviceInfoRequest from" — the only place that
    # handler exists — through its closing return, which is unique.
    block = ESPHOME_CODE[
        ESPHOME_CODE.index("DeviceInfoRequest from"):
        ESPHOME_CODE.index("if isinstance(msg, api_pb2.ListEntitiesRequest):")
    ]
    assert "ESPHOME_DEVICE_MODEL" not in block, (
        "DeviceInfoResponse still references the old constant — every "
        "device reads the same model regardless of board"
    )
    assert "esphome_device_model(" in block
    assert "_owning_server.board_id" in block
    # The two fields HA displays must reference the same resolved string,
    # not duplicate the call — a drift between them would present a
    # different Model under "Device info" vs HA's parsing of project_name.
    assert block.count("model=_model") == 1
    assert block.count("f\"EchoMuse.{_model}\"") == 1


def test_mdns_publishes_the_same_model_string():
    """
    mDNS's project_name has to agree with what DeviceInfoResponse.model
    says — HA keys discovery on the mac, but the project_name is what the
    config flow's re-import step uses. Drift here is a discovery-time
    mismatch that looks like a different device on a Radar install.
    """
    block = ESPHOME_CODE[ESPHOME_CODE.index("def _make_device_mdns_info"):]
    block = block[: block.index("\n\n")]
    assert "ESPHOME_DEVICE_MODEL" not in block, (
        "mDNS builder still references the constant; HA's discovery TXT "
        "would not follow the device's actual board"
    )
    assert "esphome_device_model(board_id)" in block


def test_ble_proxy_also_routes_through_the_helper():
    """
    The BT proxy shares the same Model display in HA. It is per-device
    but not per-board (one proxy per Echo regardless of board), so it
    passes None and falls back to the biscuit string — the important
    thing is that it goes through the same table, so a future board
    change touches one place.
    """
    from em_ble_proxy import _device_model as bt_model
    import em_esphome
    assert bt_model() == em_esphome.esphome_device_model(None)


# ── The wire response ────────────────────────────────────────────────────────


def _satellite(board_id: str | None):
    """
    An EchoMuseSatellite with a fake owning server, ready to answer
    DeviceInfoRequest. The owning server carries `board_id` (the only
    field the model path reads) and `capabilities` (read by the
    voice-assistant flag path further down — empty list is fine, we are
    testing the model field).
    """
    sat = em_esphome.EchoMuseSatellite(
        device_id="G090LF0000000001",
        label="Test",
        mac_address="02:EC:00:00:00:01",
        oww_model_id="hey_jarvis_v0.1",
        on_disconnected_cb=lambda *_: None,
        oww_model_info=None,
        owning_server=SimpleNamespace(
            board_id=board_id, capabilities=[]),
    )
    return sat


def _device_info(sat):
    """Drive a DeviceInfoRequest through handle_message and return the one
    response it yields."""
    responses = list(sat.handle_message(api_pb2.DeviceInfoRequest()))
    assert len(responses) == 1, (
        f"DeviceInfoRequest should yield exactly one response, got {responses!r}"
    )
    return responses[0]


@pytest.mark.parametrize("board_id,expected", [
    ("biscuit", "Echo Dot Gen 2 (biscuit)"),
    ("radar",   "Echo 2 (radar)"),
    (None,      "Echo Dot Gen 2 (biscuit)"),
])
def test_device_info_reports_the_per_board_model(board_id, expected):
    """
    End-to-end: the wire response carries the right `model` for each
    board. HA reads this field to fill "Device info" alongside the
    manufacturer.
    """
    info = _device_info(_satellite(board_id))
    assert info.model == expected


@pytest.mark.parametrize("board_id,expected", [
    ("biscuit", "EchoMuse.Echo Dot Gen 2 (biscuit)"),
    ("radar",   "EchoMuse.Echo 2 (radar)"),
])
def test_project_name_agrees_with_model_and_carries_the_dot(board_id, expected):
    """
    HA's ESPHome integration displays the segment after the dot in
    project_name as the device Model (overriding DeviceInfo's model
    field), and its manager.py splits on '.' unconditionally — a missing
    dot raises IndexError and the device never appears in Devices &
    Services. project_name therefore has to mirror model and always
    contain a dot.
    """
    info = _device_info(_satellite(board_id))
    assert info.project_name == expected
    assert "." in info.project_name


def test_a_radar_device_no_longer_reports_as_biscuit():
    """
    The bug being fixed: a Radar device shipped "Echo Dot Gen 2 (biscuit)"
    to HA regardless of its actual board. The current constant is what
    every shipped GUI saw — a regression test pins it.
    """
    info = _device_info(_satellite("radar"))
    assert "biscuit" not in info.model, (
        "a Radar would still be presented as an Echo Dot Gen 2 in HA"
    )
    assert "radar" in info.model


def test_a_biscuit_device_still_reports_as_biscuit():
    """
    The change must not alter what a Dot 2 sees — its fleet has been
    looking at "Echo Dot Gen 2 (biscuit)" since the project's start.
    """
    info = _device_info(_satellite("biscuit"))
    assert info.model == "Echo Dot Gen 2 (biscuit)"


# ── mDNS refresh on connect ──────────────────────────────────────────────────


def test_device_connected_refreshes_mdns_when_board_id_arrives():
    """
    At startup the Echo hasn't connected yet, so mDNS was published with
    the biscuit default. When device_connected() learns the real board,
    it must re-publish with the right project_name so HA's discovery TXT
    matches what DeviceInfoResponse says — otherwise a Radar ships
    "Echo Dot Gen 2 (biscuit)" via mDNS until the next restart.
    """
    import asyncio
    src = ESPHOME_CODE
    block = src[src.index("async def device_connected"):]
    block = block[: block.index("\nasync def device_disconnected")]
    assert "async_update_service" in block, (
        "device_connected never refreshes mDNS; a Radar installed mid-day "
        "would keep the biscuit project_name in its TXT record"
    )
    assert "_make_device_mdns_info" in block
    assert "board_id=board_id" in block


# ── No stray constant references ─────────────────────────────────────────────


def test_nothing_else_references_the_removed_constant():
    """
    The fix removes the module-level ESPHOME_DEVICE_MODEL constant and
    replaces it with a per-board helper. Anything still pointing at the
    old name would keep shipping the biscuit string on the Radar path —
    a helper nobody calls is no fix at all.
    """
    for src_path in (CONTROLLER / "em_esphome.py",
                     CONTROLLER / "em_ble_proxy.py"):
        text = src_path.read_text()
        # The lookup helper's name is the public API now; the old
        # constant name must be gone everywhere outside this helper's
        # own fallback definition.
        assert "ESPHOME_DEVICE_MODEL" not in text, (
            f"{src_path.name} still references ESPHOME_DEVICE_MODEL — "
            "rename it to esphome_device_model(...)"
        )