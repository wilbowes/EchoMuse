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


def parse(text: str) -> tuple | None:
    """
    ("v2.10.0", "controller-v2.10.0", "v2.10.0-3-gabc1234-dirty") -> (2, 10, 0).

    None for anything that is not a version at all ("dev", a bare source
    copy). Callers must treat that as "cannot compare" rather than as zero —
    a controller that does not know its own version has no business claiming
    to be out of date.
    """
    text = (text or "").strip().removeprefix(_PREFIX).lstrip("v")
    if not text:
        return None
    parts = text.split("-")[0].split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts[:3])


def compare(current: str, latest: str) -> dict:
    """
    Is `latest` newer than the running `current`?

    Three outcomes, and the distinction matters more than a boolean would:
      - "update"  — a strictly newer version exists.
      - "current" — running the newest, or a build AHEAD of it. A local build
                    between tags describes as v2.10.0-3-gabc1234, whose base
                    parses equal to v2.10.0, so a `>` test alone is right here
                    only because it is strict; anything looser would claim an
                    update forever on a dev checkout.
      - "unknown" — the running version is not comparable ("dev"). Say so
                    rather than guessing: a false "up to date" is worse than
                    an honest shrug, because it is the answer that stops
                    someone looking.
    """
    c, l = parse(current), parse(latest)
    if c is None or l is None:
        return {"status": "unknown", "available": False}
    if l > c:
        return {"status": "update", "available": True}
    return {"status": "current", "available": False}


# ─── Device firmware channels ────────────────────────────────────────────────
#
# Firmware is released as vX.Y.Z (GA) or vX.Y.Z-ea.N (Early Access, published
# as a GitHub prerelease). GA controllers offer only GA firmware; EA and dev
# controllers offer both. parse() above cannot tell 2.17.0-ea.1 from 2.17.0,
# and the firmware check used to be a string comparison, which on a GA
# controller offered a device running EA firmware the older GA as an
# "update". firmware_key orders all of them.

def firmware_key(text: str) -> tuple | None:
    """
    An ordering key for a firmware version, or None when it is not one (a
    timestamped dev build such as "20260926-1044-dev").

      v2.17.0-ea.1  <  v2.17.0-ea.2  <  v2.17.0  <  v2.17.0-3-gabc1234

    The last is a build three commits past the tag (git describe), so it is
    ahead of the release it was built from.
    """
    t = (text or "").strip().lstrip("v")
    parts = t.split("-")
    nums = parts[0].split(".")
    if len(nums) != 3 or not all(n.isdigit() for n in nums):
        return None
    rest = parts[1:]
    stage, ea = 1, 0
    if rest and rest[0].startswith("ea."):
        n = rest[0][3:]
        if not n.isdigit():
            return None
        stage, ea = 0, int(n)
        rest = rest[1:]
    commits = 0
    if rest and rest[0].isdigit():
        commits = int(rest[0])
    return (int(nums[0]), int(nums[1]), int(nums[2]), stage, ea, commits)


def firmware_is_ea(text: str) -> bool:
    k = firmware_key(text)
    return k is not None and k[3] == 0


def firmware_update(device: str | None, latest: str | None) -> bool:
    """
    Whether `latest` is an update for a device running `device`. A device
    ahead of it (EA firmware on a GA controller, or a build past the tag) is
    not offered it. When either is not a version, the old rule stands: any
    difference is an update, which is what moves a dev build onto a release.
    """
    if not device or not latest:
        return False
    d, l = firmware_key(device), firmware_key(latest)
    if d is None or l is None:
        return device != latest
    return l > d


def offers_ea_firmware(controller_version: str = VERSION) -> bool:
    """
    Only a GA controller build (a plain X.Y.Z) is limited to GA firmware.
    EA builds (2.25.0-ea.1), builds between tags and "dev" are not.
    """
    t = (controller_version or "").strip().removeprefix(_PREFIX).lstrip("v")
    nums = t.split(".")
    return not (len(nums) == 3 and all(n.isdigit() for n in nums))


def choose_firmware_release(releases: list, include_ea: bool) -> dict | None:
    """
    The newest firmware release this controller may offer, from GitHub's
    release list: published, a v* tag, carrying the `server` binary. A GA
    controller skips prereleases AND anything named -ea.N, so neither a
    mislabelled release nor a missing flag can put EA firmware on the GA
    fleet. Chosen by version, not list order.
    """
    best, best_key = None, None
    for r in releases or []:
        tag = r.get("tag_name", "")
        if r.get("draft") or not tag.startswith("v"):
            continue
        if not include_ea and (r.get("prerelease") or firmware_is_ea(tag)):
            continue
        if not any(a.get("name") == "server" for a in r.get("assets", [])):
            continue
        k = firmware_key(tag)
        if k is None:
            continue
        if best_key is None or k > best_key:
            best, best_key = r, k
    return best
