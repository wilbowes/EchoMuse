"""
tools/uat/rig.py: the two faults the 2.26.0 acceptance run hit (#861).

`up` never started the browser container that `run_controls.py` connects to,
and `attach` moved an Echo's credentials aside before finding out the rig
already held its row — the approval then answered 409 and left the Echo with
neither controller.

The rig is a dev-box tool with no Docker in CI, so these drive the command
building and the API calls in place; nothing here starts a container.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "uat"))

import rig  # noqa: E402

SERIAL = "G090LF1234567890"


class _Docker:
    def __init__(self):
        self.calls = []

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _conflict(code: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://rig", 409, "Conflict", {},
        io.BytesIO(json.dumps({"code": code}).encode()))


def test_up_starts_the_browser_beside_the_controller(tmp_path, monkeypatch):
    docker = _Docker()
    monkeypatch.setattr(rig, "subprocess", docker)
    monkeypatch.setattr(rig, "STATE", tmp_path)
    monkeypatch.setattr(rig, "wait_for", lambda *a, **kw: "token")
    monkeypatch.setattr(rig, "api", lambda *a, **kw: {"token": "tok"})

    rig.up("ghcr.io/x/controller:1")

    ran = [c[c.index("--name") + 1] for c in docker.calls if c[:2] == ["docker", "run"]]
    assert ran == [rig.CONTAINER, rig.BROWSER]
    removed = [c[3] for c in docker.calls if c[:3] == ["docker", "rm", "-f"]]
    assert removed == [rig.CONTAINER, rig.BROWSER]

    browser = docker.calls[-1]
    assert rig.PLAYWRIGHT_IMAGE in browser
    assert browser[browser.index("--network") + 1] == "host"
    assert browser[browser.index("--ipc") + 1] == "host"
    assert "playwright run-server --port 3111" in browser[-1]


def test_attach_does_not_touch_an_echo_the_rig_already_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(rig, "STATE", tmp_path)
    monkeypatch.setattr(rig, "device", lambda serial: {
        "device_id": serial, "approved": True, "connected": True,
        "firmware_ver": "2.1.0"})
    monkeypatch.setattr(rig, "Echo", lambda serial: pytest.fail("moved its credentials"))
    monkeypatch.setattr(rig, "api", lambda *a, **kw: pytest.fail("approved it again"))

    rig.attach(SERIAL)

    assert rig._state()["serials"] == [SERIAL]


def test_an_approval_another_attach_won_is_success(monkeypatch):
    def api(method, path, body=None, token=None):
        raise _conflict("already_approved")
    monkeypatch.setattr(rig, "api", api)

    rig.approve(SERIAL)


def test_a_duplicate_label_is_still_a_failure(monkeypatch):
    def api(method, path, body=None, token=None):
        raise _conflict("duplicate_label")
    monkeypatch.setattr(rig, "api", api)

    with pytest.raises(urllib.error.HTTPError):
        rig.approve(SERIAL)
