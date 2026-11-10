"""
Every workflow input that can change under us is pinned (CLAUDE.md →
"Pipeline hygiene"). Actions were pinned by SHA and images by digest on
2026-09-23, but the runner label stayed `ubuntu-latest`; GitHub moved it to
26.04 in October 2026, which has no qemu-user-static, and the CPU baseline
check failed on main and on every PR until the label was pinned.
"""

import re
from pathlib import Path

WORKFLOWS = sorted((Path(__file__).resolve().parents[2] / ".github" / "workflows").glob("*.yml"))


def test_workflows_found():
    assert WORKFLOWS, "no workflows found; the guards below would pass vacuously"


def test_runner_labels_are_versioned():
    floating = []
    for wf in WORKFLOWS:
        for n, line in enumerate(wf.read_text().splitlines(), 1):
            m = re.search(r"\b(?:runs-on|runner):\s*(\S+)", line)
            if m and m.group(1).endswith("-latest"):
                floating.append(f"{wf.name}:{n}: {m.group(1)}")
    assert not floating, f"runner labels must name a release, not -latest: {floating}"


def test_actions_are_pinned_by_sha():
    unpinned = []
    for wf in WORKFLOWS:
        for n, line in enumerate(wf.read_text().splitlines(), 1):
            m = re.search(r"\buses:\s*(\S+)", line)
            if m and not m.group(1).startswith("./") and not re.search(r"@[0-9a-f]{40}$", m.group(1)):
                unpinned.append(f"{wf.name}:{n}: {m.group(1)}")
    assert not unpinned, f"actions must be pinned by commit SHA: {unpinned}"
