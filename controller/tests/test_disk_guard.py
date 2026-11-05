"""
#995: free space on the data volume, and the writes that stop before it fills.

The controller's own writes are bounded, but an add-on's `/data` is Home
Assistant's partition — HA's database or backups can fill it under us. Two
marks: below 500 MB the optional writes stop, below 100 MB everything new
stops except the database's small writes. Both are module constants in
em_disk and every predicate takes the database path, so these tests point it
at a measured size instead of a real full disk.
"""

import asyncio
import logging

import pytest

import em_disk
import em_firmware
import em_recordings
import em_wake_samples

MB = 1048576


class _Usage:
    def __init__(self, free_mb):
        self.free = int(free_mb * MB)
        self.used = 0
        self.total = self.free


@pytest.fixture
def free_space(monkeypatch, tmp_path):
    """Set the free space every predicate sees, beside a tmp database."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))

    def set_free(mb):
        monkeypatch.setattr(em_disk.shutil, "disk_usage",
                            lambda path: _Usage(mb))

    return set_free


def _pcm(ms):
    return b"\x00\x01" * int(em_recordings.SAMPLE_RATE * ms / 1000)


# ── the marks ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("free,optional,new", [
    (499, False, True),    # below 500: optional writes stop, new ones do not
    (500, True, True),     # at the mark: everything resumes
    (99, False, False),    # below 100: nothing new
    (100, False, True),    # at the mark: only the optional set stays stopped
])
def test_each_mark_is_exclusive_on_the_low_side(free_space, free, optional, new):
    free_space(free)
    assert em_disk.optional_writes_allowed() is optional
    assert em_disk.new_writes_allowed() is new


def test_the_default_volume_is_the_database_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "echomuse.db"))
    seen = []
    monkeypatch.setattr(em_disk.shutil, "disk_usage",
                        lambda path: seen.append(path) or _Usage(700))
    assert em_disk.optional_writes_allowed()
    assert seen == [str(tmp_path / "data")]


def test_a_predicate_can_be_pointed_at_another_volume(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(em_disk.shutil, "disk_usage",
                        lambda path: seen.append(path) or _Usage(50))
    assert em_disk.new_writes_allowed(str(tmp_path / "other" / "db")) is False
    assert seen == [str(tmp_path / "other")]


def test_an_unreadable_volume_does_not_block_writes(monkeypatch):
    def fail(path):
        raise OSError("no such mount")

    monkeypatch.setattr(em_disk.shutil, "disk_usage", fail)
    assert em_disk.free_mb() == float("inf")
    assert em_disk.optional_writes_allowed()
    assert em_disk.new_writes_allowed()


def test_a_mark_is_logged_once_and_recovery_logs_again(free_space, caplog,
                                                       monkeypatch):
    import em_process_stats

    monkeypatch.setattr(em_process_stats, "_last_disk_state", None)
    def marks(level):
        return [r for r in caplog.records
                if r.name == "echomuse.stats" and r.levelno == level]

    with caplog.at_level(logging.INFO, logger="echomuse.stats"):
        free_space(600)
        em_process_stats.controller_stats()
        assert not marks(logging.WARNING) and not marks(logging.INFO)

        free_space(499)
        em_process_stats.controller_stats()
        em_process_stats.controller_stats()
        warnings = marks(logging.WARNING)
        assert len(warnings) == 1
        assert "optional writes are paused" in warnings[0].message

        free_space(600)
        em_process_stats.controller_stats()
        infos = marks(logging.INFO)
        assert len(infos) == 1
        assert "writes resumed" in infos[0].message

        free_space(50)
        em_process_stats.controller_stats()
        warnings = marks(logging.WARNING)
        assert len(warnings) == 2
        assert "only database writes continue" in warnings[1].message


# ── the write paths ──────────────────────────────────────────────────────────


def test_an_utterance_is_skipped_and_says_why(free_space, tmp_path, caplog):
    db = str(tmp_path / "echomuse.db")
    free_space(499)
    with caplog.at_level(logging.WARNING, logger="echomuse.recordings"):
        assert em_recordings.save("dev1", 7, _pcm(200), db_path=db) is None
    assert "below the 500 MB mark" in caplog.text
    assert not em_recordings.recordings_dir(db).exists()

    free_space(500)
    assert em_recordings.save("dev1", 7, _pcm(200), db_path=db) == "dev1_7.wav"


def test_a_wake_clip_is_skipped_and_says_why(free_space, tmp_path, caplog):
    db = str(tmp_path / "echomuse.db")
    name = em_wake_samples.filename("dev1")
    free_space(499)
    with caplog.at_level(logging.WARNING, logger="echomuse.wakesamples"):
        assert em_wake_samples.save("dev1", name, _pcm(80), db_path=db) is False
    assert "below the 500 MB mark" in caplog.text
    assert not em_wake_samples.samples_dir(db).exists()

    free_space(500)
    assert em_wake_samples.save("dev1", name, _pcm(80), db_path=db) is True


def test_archiving_a_wake_clip_is_a_new_write(free_space, tmp_path, caplog):
    db = str(tmp_path / "echomuse.db")
    name = em_wake_samples.filename("dev1")
    free_space(500)
    assert em_wake_samples.save("dev1", name, _pcm(80), db_path=db) is True

    free_space(99)
    with caplog.at_level(logging.WARNING, logger="echomuse.wakesamples"):
        assert em_wake_samples.archive("dev1", name, "wake", {}, db_path=db) is False
    assert "below the 100 MB mark" in caplog.text
    assert not em_wake_samples.archive_dir("wake", db).exists()

    free_space(500)
    assert em_wake_samples.archive("dev1", name, "wake", {}, db_path=db) is True


def test_the_firmware_cache_is_refused_and_says_why(free_space, tmp_path, caplog):
    db = str(tmp_path / "echomuse.db")
    free_space(499)
    with caplog.at_level(logging.WARNING, logger="echomuse.firmware"):
        assert em_firmware.write("v2.11.0", b"x" * 4096, db_path=db) is False
    assert "below the 500 MB mark" in caplog.text
    assert not em_firmware.firmware_dir(db).exists()

    free_space(500)
    assert em_firmware.write("v2.11.0", b"x" * 4096, db_path=db) is True
    assert em_firmware.read("v2.11.0", db_path=db) == b"x" * 4096


def test_a_model_upload_is_refused_and_says_why(free_space, tmp_path, monkeypatch):
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import _oww_stub
    _oww_stub.install()
    try:
        import em_routes_oww
    finally:
        _oww_stub.uninstall()

    monkeypatch.setattr(em_routes_oww.em_oww_check, "problem", lambda data: None)
    handler = em_routes_oww._post_oww_model_upload.__wrapped__   # past require_admin

    async def post():
        app = web.Application()
        app.router.add_post("/upload", handler)
        async with TestClient(TestServer(app)) as client:
            form = aiohttp.FormData()
            form.add_field("model", b"x" * 1024, filename="custom.onnx")
            response = await client.post("/upload", data=form)
            return response.status, await response.json()

    free_space(499)
    status, body = asyncio.run(post())
    assert status == 507 and body["code"] == "low_disk"
    assert "500 MB" in body["error"]
    assert not (tmp_path / "oww_models").exists()

    free_space(99)
    status, body = asyncio.run(post())
    assert status == 507 and body["code"] == "disk_full"
    assert "100 MB" in body["error"]
    assert not (tmp_path / "oww_models").exists()

    free_space(500)
    status, body = asyncio.run(post())
    assert status == 201 and body["model"]["file"] == "custom.onnx"
    assert (tmp_path / "oww_models" / "custom.onnx").is_file()


def test_the_database_still_writes_when_new_files_are_refused(free_space, tmp_path):
    import em_db as db

    free_space(50)
    db.init(str(tmp_path / "echomuse.db"))
    try:
        db.set_config("disk_probe", "written")
        assert db.get_config("disk_probe") == "written"
    finally:
        db.close()
