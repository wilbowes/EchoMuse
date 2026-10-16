"""Run CI build-step shell commands with Docker output staged at its bind mount."""

import os
from pathlib import Path
import subprocess
import sys

import yaml


def test_init_build_steps_leave_artifacts_where_checks_read_them(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((repo / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["device-firmware-build"]["steps"]
    tools = tmp_path / "tools"
    tools.mkdir()
    docker = tools / "docker"
    # Simulate only the compiler's output file, mapping /emos to host emos/.
    # The actual workflow's mkdir/mv commands still run and must find it.
    docker.write_text(f"#!{sys.executable}\n" + '''
import pathlib, shlex, sys
args = shlex.split(sys.argv[-1])
output = pathlib.Path("emos") / args[args.index("-o") + 1]
output.write_bytes(b"test init")
''')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"])
    builds = [step for step in steps if step.get("name", "").startswith("Build the emOS init (")]
    assert len(builds) == 2
    for step in builds:
        result = subprocess.run(["bash", "-e", "-c", step["run"]],
                                cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    for name in ("init", "init32", "init.init_radar"):
        assert (tmp_path / "emos/build" / name).read_bytes() == b"test init"


def test_release_compiles_and_bundles_each_board_runtime(tmp_path):
    """A successful link with weak stubs must not publish a nonfunctional init."""
    import shlex
    repo = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((repo / ".github/workflows/emos-release.yml").read_text())
    steps = [s for j in workflow['jobs'].values() for s in j['steps']]
    builds = [s for s in steps if s.get('name', '').startswith('Compile init')]
    outputs = {}
    for step in builds:
        shell = step['run'].replace('\\\n', ' ')
        args = shlex.split(shlex.split(shell.split('docker run', 1)[1])[-1])
        name = Path(args[args.index('-o') + 1]).name
        board = 'radar' if name == 'init32-radar' else 'biscuit'
        assert f'-DEMOS_BOARD={board}' in args
        assert f'init/boards/boards_{board}.c' in args
        assert (repo / 'emos' / f'init/boards/boards_{board}.c').is_file()
        outputs[name] = args[0]
    assert set(outputs) == {'init', 'init32', 'init32-radar'}
    assert 'aarch64' in outputs['init']
    assert 'armv7a' in outputs['init32-radar']
    bundle = next(s['run'] for s in steps if s.get('name') == 'Bundle the payload')
    assert all(f'emos/build/{name}' in shlex.split(bundle.replace('\\\n', ' '))
               for name in outputs)
