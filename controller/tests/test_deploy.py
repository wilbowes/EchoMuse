"""
Deployment-shape guards — not logic tests. The controller Dockerfile
COPYs each module explicitly, so a new em_*.py that works fine on bare
metal crash-loops the container at import time if the COPY line is
forgotten (bitten by em_scenes.py 2026-07-10 and em_oww_models.py
2026-07-19).
"""

import re
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def test_dockerfile_copies_every_controller_module():
    dockerfile = (CONTROLLER / "Dockerfile").read_text()
    copied = set(re.findall(r"^COPY\s+(\S+\.py)\s", dockerfile, re.M))
    modules = {p.name for p in CONTROLLER.glob("em_*.py")} | {"version.py"}
    missing = sorted(modules - copied)
    assert not missing, (
        f"Dockerfile is missing COPY lines for {missing} — the container "
        f"will crash-loop at import time"
    )
