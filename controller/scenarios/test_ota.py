"""
A firmware update, end to end: the real controller pushes a binary over the
shell plane to an Echo whose shell is a real sh on a scratch /data.

The OTA's own tests read its source (test_deploy.py) or test one verdict
(test_ota_outcome.py). Neither runs the update, so a deliberate "write the
ACTIVE slot" in em_ota passed all 2,498 unit tests (test-suite review,
2026-11-16, mutant P18). These assert what a person relies on: the new
binary lands in the slot the Echo is NOT running, the running one is left
whole to roll back to, the symlink moves, and the Echo comes back on it.
"""

import os

import aiohttp

from harness import FakeEcho, wait_for

OLD = b"\x7fELF old firmware v9.9.9-0-g0000000 " + os.urandom(64 * 1024)
NEW = b"\x7fELF new firmware v9.9.10-1-gabcdef0 " + os.urandom(256 * 1024)


def _echo_fs(root):
    bin_dir = root / "data/local/bin"
    bin_dir.mkdir(parents=True)
    (root / "data/local/tmp").mkdir(parents=True)
    (bin_dir / "server_a").write_bytes(OLD)
    (bin_dir / "server").symlink_to("server_a")
    return bin_dir


def _ota_log(ctl, device_id, text):
    """What the update told the person, from the Echo's log in the dashboard."""
    return any(text in r["message"] for r in ctl.query(
        "SELECT message FROM device_logs WHERE device_id = ? AND source = 'controller'",
        device_id))


async def _push_update(ctl, device_id, binary):
    headers = {"Authorization": f"Bearer {ctl.admin_token()}"}
    base = f"http://127.0.0.1:{ctl.api_port}"
    async with aiohttp.ClientSession(headers=headers) as s:
        form = aiohttp.FormData()
        form.add_field("binary", binary, filename="server",
                       content_type="application/octet-stream")
        async with s.post(f"{base}/api/releases/upload", data=form) as r:
            assert r.status == 200, await r.text()
            token = (await r.json())["upload_token"]
        async with s.post(f"{base}/api/devices/{device_id}/update",
                          json={"upload_token": token}) as r:
            assert r.status == 202, await r.text()


def test_an_update_lands_in_the_slot_the_echo_is_not_running(scenario, tmp_path):
    async def body(ctl):
        root = tmp_path / "echo"
        bin_dir = _echo_fs(root)
        echo = FakeEcho(ctl, fs_root=root)
        await echo.connect()
        try:
            await _push_update(ctl, echo.device_id, NEW)
            await wait_for(lambda: echo.restarts >= 1, 60, "the Echo to restart")
            await wait_for(lambda: echo.version == "v9.9.10-1-gabcdef0" and any(
                r["firmware_ver"] == echo.version for r in ctl.query(
                    "SELECT firmware_ver FROM devices WHERE device_id = ?", echo.device_id)),
                30, "the Echo to register on the new firmware")

            assert (bin_dir / "server_b").read_bytes() == NEW, \
                "the new binary must land in the slot the Echo was not running"
            assert (bin_dir / "server_a").read_bytes() == OLD, \
                "the running slot must be left whole: it is the way back"
            assert os.readlink(bin_dir / "server") == "server_b"
            assert not (bin_dir / "server_b.part").exists()
            await wait_for(lambda: _ota_log(ctl, echo.device_id, "Update confirmed"), 15,
                           "the controller to record the update as confirmed")
        finally:
            await echo.close()

    scenario(body, timeout=120)


def test_a_failed_slot_migration_stops_the_update(scenario, tmp_path):
    """A legacy Echo whose `server` is not a symlink is migrated to A/B first.
    If that copy fails the update must stop with nothing written."""
    async def body(ctl):
        root = tmp_path / "echo"
        bin_dir = root / "data/local/bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "server").mkdir()   # cp refuses a directory: the migration fails
        echo = FakeEcho(ctl, fs_root=root)
        await echo.connect()
        try:
            await _push_update(ctl, echo.device_id, NEW)
            await wait_for(lambda: _ota_log(ctl, echo.device_id, "A/B migration failed"), 60,
                           "the controller to refuse the update")
            assert echo.restarts == 0, "a failed migration must not restart the Echo"
            for slot in ("server_a", "server_b"):
                assert not (bin_dir / slot).is_file() or \
                    (bin_dir / slot).read_bytes() != NEW, f"{slot} was written"
        finally:
            await echo.close()

    scenario(body, timeout=120)
