"""
version.py — controller version resolution
============================================

The controller is versioned independently of the device firmware:
device binaries are released from plain `v*` tags (embedded via
-ldflags at compile time), the controller from `controller-v*` tags
(baked into the Docker image as the EM_CONTROLLER_VERSION env var by
.github/workflows/controller-release.yml).

Resolution order:
  1. EM_CONTROLLER_VERSION env var — set in the published image; also
     the override hook for anyone building their own image.
  2. `git describe --tags --match 'controller-v*'` — bare-metal runs
     from a git checkout. The `controller-` prefix is stripped so the
     displayed form matches the image's ("v2.8.0", or
     "v2.8.0-3-gabc1234-dirty" between tags).
  3. "dev" — no env var, no git (e.g. a bare source copy).
"""

from __future__ import annotations

import os
import subprocess

_PREFIX = "controller-"


def _resolve() -> str:
    env = os.environ.get("EM_CONTROLLER_VERSION")
    if env:
        return env

    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--match", f"{_PREFIX}v*", "--dirty"],
            capture_output=True,
            text=True,
            timeout=3,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        described = out.stdout.strip()
        if out.returncode == 0 and described:
            return described.removeprefix(_PREFIX)
    except Exception:
        pass

    return "dev"


VERSION = _resolve()
