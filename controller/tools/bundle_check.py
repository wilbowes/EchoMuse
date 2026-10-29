"""
Print what em_diagnose finds in a support bundle.

    python controller/tools/bundle_check.py echomuse-support-<stamp>.json
    python controller/tools/bundle_check.py --json bundle.json

A bundle attached to an issue is someone else's data: read it here, quote
findings in a reply, and never commit it as a fixture.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import em_diagnose  # noqa: E402


def render(findings: list[dict]) -> str:
    if not findings:
        return "No findings."
    lines = []
    for f in findings:
        ev = ", ".join(f"{k}={v}" for k, v in f["evidence"].items() if v is not None)
        lines.append(f"[{f['level']}] {f['device']}: {f['text']}"
                     + (f"  ({ev})" if ev else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--json", action="store_true", help="findings as JSON")
    args = ap.parse_args(argv)
    bundle = json.loads(args.bundle.read_text())
    if bundle.get("format", 0) < 3:
        print(f"format {bundle.get('format')}: no dashboard view, so the "
              "HA, listening, link and update rules cannot run", file=sys.stderr)
    findings = em_diagnose.check(bundle)
    print(json.dumps(findings, indent=2) if args.json else render(findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
