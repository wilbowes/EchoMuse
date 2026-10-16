"""Exercise build.sh orchestration with synthetic input and stand-in tools.

The shell script and image packer are real. Compiler, strip and cpio stand-ins
keep these checks independent of the Android toolchain; they do not validate
the generated init or claim to build a bootable image.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_emos_build import arm_zimage, make_dtb, make_reference


REPO = Path(__file__).resolve().parents[2]


def executable(path, body):
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def build_tree(tmp_path):
    for name in ("emos/build.sh", "emos/mkboot.py", "emos/device/em-wifi",
                 "controller/em_emos_build.py"):
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, dest)
    boards = tmp_path / "emos/init/boards"
    boards.mkdir(parents=True)
    for board in ("biscuit", "radar"):
        (boards / f"boards_{board}.c").touch()
    tools = tmp_path / "tools"
    tools.mkdir()
    compiler = executable(tools / "clang", f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
pathlib.Path(os.environ["COMPILE_LOG"]).write_text(json.dumps(sys.argv[1:]))
pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_bytes(b"test init")
''')
    strip = executable(tools / "llvm-strip", '#!/bin/sh\nprintf stripped > "$STRIP_LOG"\n')
    # A host tool that would otherwise be selected by command -v.
    host = tmp_path / "host-tools"
    host.mkdir()
    executable(host / "llvm-strip", '#!/bin/sh\necho wrong host strip >&2\nexit 77\n')
    executable(host / "strip", '#!/bin/sh\nexit 77\n')
    executable(host / "cpio", '#!/bin/sh\ncat >/dev/null\nprintf archive\n')
    (host / "python3").symlink_to(sys.executable)
    reference = tmp_path / "reference.img"
    reference.write_bytes(make_reference(zimage=arm_zimage(), dtbs=make_dtb("radar")))
    env = os.environ.copy()
    for key in ("EMOS_BOARD", "STRIP", "WMT_PROP_SHIM"):
        env.pop(key, None)
    env.update(CC=str(compiler), PATH=str(host) + os.pathsep + env["PATH"],
               COMPILE_LOG=str(tmp_path / "compile.json"),
               STRIP_LOG=str(tmp_path / "strip.log"), EMOS_SYSTEM_PART="14",
               EMOS_VERSION="test", SUPPLICANT="/nonexistent",
               WPA_CLI="/nonexistent", BUSYBOX="/nonexistent")
    return tmp_path, env, strip


def run_build(tree):
    root, env, _ = tree
    return subprocess.run(["bash", str(root / "emos/build.sh"),
                           str(root / "reference.img"), str(root / "result.img")],
                          cwd=root, env=env, capture_output=True, text=True)


@pytest.mark.parametrize("board", [None, "radar"])
def test_compiled_board_matches_packed_board(build_tree, board):
    root, env, _ = build_tree
    if board is not None:
        env["EMOS_BOARD"] = board
    result = run_build(build_tree)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = board or "biscuit"
    args = json.loads((root / "compile.json").read_text())
    assert f"-DEMOS_BOARD={expected}" in args
    assert any(arg.endswith(f"boards_{expected}.c") for arg in args)
    cmdline = (root / "result.img").read_bytes()[64:576].split(b"\0")[0].decode()
    assert f"emos.board={expected}" in cmdline
    assert "emos.system=/dev/block/mmcblk0p14" in cmdline
    assert (root / "strip.log").read_text() == "stripped"


def test_explicit_strip_override(build_tree):
    root, env, strip = build_tree
    override = strip.rename(root / "custom-strip")
    env["STRIP"] = str(override)
    result = run_build(build_tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "strip.log").exists()


def test_missing_target_strip_fails_before_packaging(build_tree):
    root, _, strip = build_tree
    strip.unlink()
    result = run_build(build_tree)
    assert result.returncode != 0
    assert "missing target strip tool:" in result.stderr
    assert not (root / "result.img").exists()
