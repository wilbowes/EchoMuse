import em_disk


class _Usage:
    def __init__(self, free_mb):
        self.free = int(free_mb * 1024 * 1024)


def _free(monkeypatch, mb):
    monkeypatch.setattr(em_disk.shutil, "disk_usage", lambda _p: _Usage(mb))


def test_edges(monkeypatch):
    _free(monkeypatch, 501)
    assert em_disk.disk_state() == "ok"
    _free(monkeypatch, 499.9)
    assert em_disk.disk_state() == "warn"
    _free(monkeypatch, 100)
    assert em_disk.disk_state() == "warn"
    _free(monkeypatch, 99.9)
    assert em_disk.disk_state() == "refuse"


def test_optional_writes_stop_at_warn_and_resume_above(monkeypatch):
    _free(monkeypatch, 499)
    assert not em_disk.optional_writes_allowed()
    assert em_disk.new_writes_allowed()
    _free(monkeypatch, 99)
    assert not em_disk.optional_writes_allowed()
    assert not em_disk.new_writes_allowed()
    _free(monkeypatch, 900)
    assert em_disk.optional_writes_allowed()
    assert em_disk.new_writes_allowed()


def test_an_unreadable_path_is_not_refused(monkeypatch):
    def boom(_p):
        raise OSError("no such path")
    monkeypatch.setattr(em_disk.shutil, "disk_usage", boom)
    assert em_disk.disk_state() == "ok"
