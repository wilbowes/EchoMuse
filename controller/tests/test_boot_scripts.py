"""
User boot scripts (docs/boot-scripts.md): the block in start_server.sh, run by
a real shell against a temporary folder.

A mistake here runs before the firmware on every boot of every Echo, and the
device tests of a shell script are a person watching one boot. What is held:
an Echo with no folder runs nothing, only *.sh runs, and no script can hold up
the others or the server.
"""

import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "controller" / "device_payloads" / "start_server.sh"
DOC = ROOT / "docs" / "boot-scripts.md"
BOOT_D = "/data/local/etc/echomuse/boot.d"


def _block() -> str:
    m = re.search(r"^# ── User boot scripts.*?^done\n", SCRIPT.read_text(), re.S | re.M)
    assert m, "the boot scripts block has moved or changed shape"
    return m.group(0)


def _run(folder: Path, timeout: float = 10) -> str:
    """The block, with its paths moved under tmp and sup_log recording."""
    tmp = folder.parent
    body = (_block().replace(BOOT_D, str(folder))
                    .replace("/system/bin/sh", "sh")
                    .replace("/tmp/boot-", f"{tmp}/boot-"))
    stub = f'sup_log() {{ echo "$*" >> "{tmp}/sup"; }}\n'
    subprocess.run(["sh", "-c", stub + body], check=True, timeout=timeout)
    log = tmp / "sup"
    return log.read_text() if log.exists() else ""


def _wait_for(path: Path) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def _write(folder: Path, name: str, body: str) -> None:
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(body)


def test_an_echo_with_no_folder_runs_nothing(tmp_path):
    assert _run(tmp_path / "boot.d") == ""


def test_an_empty_folder_runs_nothing(tmp_path):
    folder = tmp_path / "boot.d"
    folder.mkdir()
    assert _run(folder) == ""


def test_every_script_runs_and_needs_no_execute_bit(tmp_path):
    folder = tmp_path / "boot.d"
    _write(folder, "a.sh", f': > "{tmp_path}/ran-a"\n')
    _write(folder, "b.sh", f': > "{tmp_path}/ran-b"\n')
    sup = _run(folder)
    assert _wait_for(tmp_path / "ran-a") and _wait_for(tmp_path / "ran-b")
    assert "boot script a.sh" in sup and "boot script b.sh" in sup


def test_only_sh_files_run(tmp_path):
    folder = tmp_path / "boot.d"
    _write(folder, "notes.txt", f': > "{tmp_path}/ran-txt"\n')
    _write(folder, "old.sh.bak", f': > "{tmp_path}/ran-bak"\n')
    assert _run(folder) == ""
    assert not (tmp_path / "ran-txt").exists()
    assert not (tmp_path / "ran-bak").exists()


def test_a_folder_named_like_a_script_is_skipped(tmp_path):
    folder = tmp_path / "boot.d"
    (folder / "dir.sh").mkdir(parents=True)
    assert _run(folder) == ""


def test_a_failing_script_does_not_stop_the_next(tmp_path):
    folder = tmp_path / "boot.d"
    _write(folder, "a.sh", "exit 1\n")
    _write(folder, "b.sh", f': > "{tmp_path}/ran-b"\n')
    _run(folder)
    assert _wait_for(tmp_path / "ran-b")


def test_a_hung_script_holds_up_nothing(tmp_path):
    folder = tmp_path / "boot.d"
    _write(folder, "a.sh", "sleep 5\n")
    _write(folder, "b.sh", f': > "{tmp_path}/ran-b"\n')
    _run(folder, timeout=3)
    assert _wait_for(tmp_path / "ran-b")


def test_a_script_logs_to_its_own_file(tmp_path):
    folder = tmp_path / "boot.d"
    _write(folder, "a.sh", 'echo out; echo err >&2; : > "$0.done"\n')
    _run(folder)
    assert _wait_for(folder / "a.sh.done")
    assert (tmp_path / "boot-a.sh.log").read_text().split() == ["out", "err"]


def test_the_scripts_run_before_the_server_starts():
    src = SCRIPT.read_text()
    assert src.index(_block()) < src.index("/data/local/bin/server >> /tmp/server.log")


def test_the_doc_names_the_folder_the_script_reads():
    assert BOOT_D in _block() and BOOT_D in DOC.read_text()
