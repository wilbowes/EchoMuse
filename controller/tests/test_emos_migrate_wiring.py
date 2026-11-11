"""The migration's controller side: the FireOS connect check, the kept FireOS
image, and the two routes."""

import asyncio
import hashlib
import types

import pytest

import em_emos_migrate as mg
import em_emos_ota as ota
import em_emos_update as up
import test_emos_migrate_flow as mf

pytestmark = getattr(mf, "pytestmark", [])


def settle(dev):
    async def sh(cmd, timeout):
        return dev.run(cmd)
    return asyncio.run(mg.settle_on_fireos(sh))


# ── What a FireOS connect finds ──────────────────────────────────────────────

def test_an_echo_that_never_migrated_says_nothing(tmp_path):
    dev = mf.FireOS(tmp_path)
    for p in (dev.root / "data/emos").iterdir():
        p.unlink()
    (dev.root / "data/emos").rmdir()
    assert mg.settle_verdict(dev.run(mg.settle_cmd()))[0] == "none"
    assert settle(dev) == ("", "info")


def test_written_but_not_restarted_keeps_its_trial(tmp_path):
    """A controller restart between the write and the restart: emOS is on the
    partition, FireOS is running, and the mark must stay for the first boot."""
    dev = mf.FireOS(tmp_path)
    dev.ignore_reboot = True
    with pytest.raises(up.Refused):
        asyncio.run(mg.run_migration(mf._Gone(dev)))   # written, never restarted
    assert dev.mark.exists() and up.is_ours(dev.image())
    msg, level = settle(dev)
    assert level == "info" and "starts at its next restart" in msg
    assert dev.mark.exists()


def test_a_rollback_nobody_watched_is_reported_and_tidied(tmp_path):
    dev = mf.FireOS(tmp_path)
    dev.new_image_works = False
    with pytest.raises(up.Refused):
        mf.run(mf.IO(dev))
    # As if the controller had been down: init's record still in place.
    rec = dev.root / "data/emos/rollback.last"
    rec.write_text(f"from={'a' * 40}\nto={'b' * 40}\ntries=3\n")
    msg, level = settle(dev)
    assert level == "warn" and "rolled back after 3" in msg
    assert not rec.exists()
    assert settle(dev) == ("", "info")


def test_a_migration_that_stopped_part_way_is_tidied(tmp_path):
    dev = mf.FireOS(tmp_path)
    (dev.root / "data/emos/boot-new.img").write_bytes(b"x" * 4096)
    msg, level = settle(dev)
    assert level == "warn" and "did not complete" in msg
    assert not (dev.root / "data/emos/boot-new.img").exists()


def test_an_unfinished_reply_decides_nothing():
    assert mg.settle_verdict("") == ("unknown", {})


# ── The kept FireOS image ────────────────────────────────────────────────────

def test_the_fireos_image_is_kept_only_once_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    data = b"FIREOS" * 1000
    md5 = hashlib.md5(data).hexdigest()
    assert ota.escrow_path("G090TESTDOT2000A") is None
    assert ota.write_escrow("G090TESTDOT2000A", data, md5) == ""
    p = ota.escrow_path("G090TESTDOT2000A")
    assert p.read_bytes() == data
    assert (p.parent / "G090TESTDOT2000A.img.md5").read_text().strip() == md5
    # A copy that does not match never replaces the good one.
    assert "read back" in ota.write_escrow("G090TESTDOT2000A", b"other", md5)
    assert p.read_bytes() == data
    assert not (p.parent / "G090TESTDOT2000A.img.part").exists()
    # The id becomes a file name, so only a plain one is accepted.
    assert ota.write_escrow("../x", data, md5)
    assert ota.escrow_path("../x") is None


def test_the_dashboard_view_knows_the_image_and_the_board(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    monkeypatch.setattr(ota, "_escrowed", None)
    monkeypatch.setattr(ota.em_state, "boards",
                        {"G090TESTDOT2000A": "biscuit", "G000TESTRADAR00B": "radar"})
    assert ota.migrate_view("G090TESTDOT2000A") == {"image": False, "why": None}
    # The button is off on a board the route refuses, with the reason.
    assert "Echo Dot 2" in ota.migrate_view("G000TESTRADAR00B")["why"]
    assert ota.migrate_view("unknown")["why"]
    # A kept image shows from the moment it is written, with no new listing.
    data = b"FIREOS" * 100
    assert ota.write_escrow("G090TESTDOT2000A", data, hashlib.md5(data).hexdigest()) == ""
    assert ota.migrate_view("G090TESTDOT2000A")["image"] is True
    # And a fresh start finds it on disk.
    monkeypatch.setattr(ota, "_escrowed", None)
    assert ota.has_escrow("G090TESTDOT2000A")


# ── The routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def routes(monkeypatch, tmp_path):
    import em_db as db
    import em_releases
    import em_routes_updates as ru
    import em_state
    import em_tasks

    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    monkeypatch.setattr(db, "get_device", lambda d: {"device_id": d} if d != "nobody" else None)
    live = types.SimpleNamespace(base_os="fireos")
    monkeypatch.setattr(em_state, "devices", {"E1": live})
    monkeypatch.setattr(em_state, "boards", {"E1": "biscuit"})

    async def fetch():
        return {"version": "emos-v0.11"}
    monkeypatch.setattr(em_releases, "fetch_emos", fetch)
    started = []
    monkeypatch.setattr(em_tasks, "spawn", lambda coro: (started.append(coro), coro.close()))
    monkeypatch.setattr(ota, "run_migration", lambda d: asyncio.sleep(0))
    return types.SimpleNamespace(ru=ru, live=live, state=em_state, started=started)


def _call(handler, method, path, route):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app = web.Application()
        app.router.add_route(method, route, handler.__wrapped__)
        async with TestClient(TestServer(app)) as c:
            r = await c.request(method, path)
            body = await r.read()
            return r.status, body
    return asyncio.run(go())


def migrate(r, dev="E1"):
    import json
    status, body = _call(r.ru._post_device_emos_migrate, "POST",
                         f"/api/devices/{dev}/emos_migrate",
                         "/api/devices/{id}/emos_migrate")
    return status, json.loads(body)


def test_migrate_starts_on_a_fireos_dot2(routes):
    status, body = migrate(routes)
    assert status == 202 and body["version"] == "emos-v0.11"
    assert len(routes.started) == 1


@pytest.mark.parametrize("change,code", [
    (lambda r: r.state.devices.clear(), "device_offline"),
    (lambda r: setattr(r.live, "base_os", "emos"), "not_fireos"),
    (lambda r: setattr(r.live, "base_os", None), "not_fireos"),
    (lambda r: r.state.boards.update(E1="radar"), "not_supported"),
    (lambda r: r.state.boards.clear(), "not_supported"),
    (lambda r: ota.in_progress.add("E1"), "update_in_progress"),
])
def test_migrate_refuses_what_the_controller_can_know(routes, change, code):
    change(routes)
    try:
        status, body = migrate(routes)
    finally:
        ota.in_progress.discard("E1")
    assert status == 409 and body["code"] == code
    assert routes.started == []


def test_migrate_refuses_an_unknown_device(routes):
    status, body = migrate(routes, "nobody")
    assert status == 404


def test_the_kept_image_downloads(routes):
    route = "/api/devices/{id}/fireos_image"
    status, _ = _call(routes.ru._get_device_fireos_image, "GET",
                      "/api/devices/E1/fireos_image", route)
    assert status == 404
    data = b"FIREOS" * 100
    assert ota.write_escrow("E1", data, hashlib.md5(data).hexdigest()) == ""
    status, body = _call(routes.ru._get_device_fireos_image, "GET",
                         "/api/devices/E1/fireos_image", route)
    assert status == 200 and body == data
