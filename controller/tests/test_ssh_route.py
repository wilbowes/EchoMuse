"""
POST /api/devices/{id}/ssh, the only writer of an Echo's SSH settings.

Driven through the real handler (auth unwrapped), with the database and the
live device stubbed: what is stored, what is pushed, and what is refused.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
from aiohttp.test_utils import make_mocked_request  # noqa: E402

import em_routes_devices as routes  # noqa: E402

KEY = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG6Uo7lhFNVb4c3wJ4yWJl3c3H4ox0Q"
       "kYjpZrR4P9aN7 me@host")


class _DB:
    def __init__(self):
        self.config = {"owwModel": "hey_mycroft"}

    def get_device(self, device_id):
        return {"device_id": device_id} if device_id == "dev" else None

    def get_device_config(self, device_id):
        return dict(self.config)

    def set_device_config(self, device_id, cfg):
        self.config = dict(cfg)

    def get_effective_device_config(self, device_id):
        return dict(self.config)


@pytest.fixture
def env(monkeypatch):
    db = _DB()
    pushed, events = [], []
    devices = {}
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes.em_state, "devices", devices)

    async def apply(device_id, live, cfg):
        pushed.append(cfg)

    async def push(ev):
        events.append(ev)

    monkeypatch.setattr(routes.em_device_config, "apply_live_config", apply)
    monkeypatch.setattr(routes.em_events, "push", push)
    return SimpleNamespace(db=db, pushed=pushed, events=events, devices=devices)


def _post(body, device_id="dev"):
    req = make_mocked_request("POST", f"/api/devices/{device_id}/ssh",
                              match_info={"id": device_id})

    async def _json():
        return body
    req.json = _json
    resp = asyncio.run(routes._post_device_ssh.__wrapped__(req))
    return resp.status, json.loads(resp.text)


def test_on_stores_normalised_keys_and_pushes_to_the_live_echo(env):
    env.devices["dev"] = SimpleNamespace(capabilities=["ssh"])
    status, body = _post({"enabled": True,
                          "authorized_keys": "# mine\r\n" + KEY + "  \r\n"})
    assert status == 200, body
    assert env.db.config["sshEnabled"] is True
    assert env.db.config["sshAuthorizedKeys"] == KEY + "\n"
    assert env.db.config["owwModel"] == "hey_mycroft", "other keys kept"
    assert env.pushed and env.pushed[-1]["sshEnabled"] is True
    assert body["authorized_keys"] == KEY + "\n", "the form shows what is stored"


def test_off_keeps_the_keys(env):
    env.devices["dev"] = SimpleNamespace(capabilities=["ssh"])
    _post({"enabled": True, "authorized_keys": KEY})
    status, _ = _post({"enabled": False, "authorized_keys": KEY})
    assert status == 200
    assert env.db.config["sshEnabled"] is False
    assert env.db.config["sshAuthorizedKeys"] == KEY + "\n"


def test_on_with_no_key_is_refused(env):
    status, body = _post({"enabled": True, "authorized_keys": "# none\n"})
    assert status == 400 and "sshEnabled" not in env.db.config


def test_a_bad_key_is_refused_and_names_the_line(env):
    status, body = _post({"enabled": True,
                          "authorized_keys": KEY + "\nssh-rsa AAAA x"})
    assert status == 400 and "line 2" in json.dumps(body)
    assert "sshEnabled" not in env.db.config


def test_a_live_echo_without_the_capability_is_refused(env):
    env.devices["dev"] = SimpleNamespace(capabilities=["mic", "speaker"])
    status, _ = _post({"enabled": True, "authorized_keys": KEY})
    assert status == 409 and "sshEnabled" not in env.db.config


def test_an_offline_echo_is_stored_for_when_it_connects(env):
    status, body = _post({"enabled": True, "authorized_keys": KEY})
    assert status == 200 and body.get("pushed") is False
    assert env.db.config["sshEnabled"] is True and not env.pushed


@pytest.mark.parametrize("body", [
    {"enabled": "yes", "authorized_keys": KEY},
    {"authorized_keys": KEY},
    {"enabled": True, "authorized_keys": 5},
])
def test_wrong_types_are_refused(env, body):
    assert _post(body)[0] == 400


def test_an_unknown_device_is_404(env):
    assert _post({"enabled": False, "authorized_keys": ""}, "nope")[0] == 404
