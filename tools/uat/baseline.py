#!/usr/bin/env python3
"""Check or apply the UAT config baseline on a controller.

The baseline is the controller's shipped defaults plus the exceptions in
baseline.json, with every UAT Echo following the fleet. rig.py up applies it,
run_controls.py refuses to start without it, and the report records its hash,
so every UAT starts from the same config and says which.

    python3 baseline.py --check                       # the rig (rig.py up)
    python3 baseline.py --check --url URL --token T   # any controller
    python3 baseline.py --apply --echo 1VVV --echo 15LE  # then checks again

--echo takes the last four characters of a serial; without it every
connected Echo is checked. --apply writes only what differs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "controller"))

# Per-Echo state, not settings: kept on the Echo when an override is cleared,
# and re-sent so the controller's would-drop guard does not refuse the clear.
from em_config_sections import STATE_KEYS  # noqa: E402
from em_db import DEFAULT_DEVICE_CONFIG  # noqa: E402


def load(path: Path = HERE / "baseline.json") -> dict:
    return json.loads(path.read_text())


def desired(spec: dict, defaults: dict = DEFAULT_DEVICE_CONFIG) -> dict:
    """Fleet config the baseline asks for, site keys excluded."""
    site = set(spec["site_keys"]["keys"])
    want = {k: v for k, v in defaults.items() if k not in site}
    want.update({k: e["value"] for k, e in spec["exceptions"].items()})
    return want


def digest(want: dict) -> str:
    return hashlib.sha256(json.dumps(want, sort_keys=True).encode()).hexdigest()[:12]


def fleet_diff(want: dict, fleet: dict) -> dict:
    """{key: (have, want)} for every baseline key the fleet has wrong."""
    return {k: (fleet.get(k), v) for k, v in want.items() if fleet.get(k) != v}


class Controller:
    def __init__(self, url: str, token: str, cookie: str | None = None):
        self.url, self.token, self.cookie = url.rstrip("/"), token, cookie

    def __call__(self, method: str, path: str, body: dict | None = None):
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.token}"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(
            self.url + path, method=method, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        return json.loads(raw) if raw else None


def _echoes(api, suffixes: list[str]) -> list[str]:
    ids = [d["device_id"] for d in api("GET", "/api/devices") if d.get("connected")]
    if not suffixes:
        return ids
    picked = [i for i in ids if any(i.endswith(s) for s in suffixes)]
    missing = [s for s in suffixes if not any(i.endswith(s) for i in picked)]
    if missing:
        sys.exit(f"not connected: {', '.join(missing)}")
    return picked


def check(api, suffixes: list[str] = ()) -> tuple[str, list[str]]:
    """(baseline hash, one line per difference); no differences = at baseline."""
    want = desired(load())
    resp = api("GET", "/api/global/config")
    fleet = resp.get("config", resp)
    out = [f"fleet {k}: {have!r}, baseline {w!r}"
           for k, (have, w) in sorted(fleet_diff(want, fleet).items())]
    for dev in _echoes(api, list(suffixes)):
        sections = api("GET", f"/api/devices/{dev}/config")["config_sections"]
        if sections:
            out.append(f"{dev[-4:]} overrides {', '.join(sections)}")
    return digest(want), out


def apply(api, suffixes: list[str] = ()) -> None:
    """Write only what differs: the fleet keys, then clear each Echo's overrides."""
    want = desired(load())
    resp = api("GET", "/api/global/config")
    fleet = resp.get("config", resp)
    if fleet_diff(want, fleet):
        # Read-modify-write: the POST replaces, and site keys must survive.
        api("POST", "/api/global/config", {**fleet, **want})
    for dev in _echoes(api, list(suffixes)):
        cfg = api("GET", f"/api/devices/{dev}/config")
        if cfg["config_sections"]:
            keep = {k: cfg["config"][k] for k in STATE_KEYS if k in cfg["config"]}
            api("POST", f"/api/devices/{dev}/config", {"config_sections": [], **keep})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--echo", action="append", default=[], help="last four of a serial")
    ap.add_argument("--url", help="controller base URL (default: the rig)")
    ap.add_argument("--token", help="bearer token (default: the rig's)")
    ap.add_argument("--cookie", help="extra Cookie header, e.g. HA ingress_session=...")
    a = ap.parse_args()
    if a.url:
        api = Controller(a.url, a.token or "", a.cookie)
    else:
        import rig
        api = rig.api
    if a.apply:
        apply(api, a.echo)
    h, diffs = check(api, a.echo)
    print(f"baseline {h}")
    print("\n".join(f"  {d}" for d in diffs) or "  at baseline")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
