"""
Release UAT report: docs/uat-results/<version>.md, from the run's JSON.

    python report.py VERSION --controls controls.json --a11y a11y.json
                     [--human human.json]

The report is the record that ships with a release (Wil, 2026-09-26): what
every dashboard control was seen to do, the accessibility scan, which actions
nothing checked, and the outcome of each guided human step.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import inventory

ROOT = Path(__file__).resolve().parents[2]

MEANING = {
    "applied":  "the Echo is running the new value",
    "received": "the Echo received it; the effect needs a person or is controller-side",
    "api":      "stored, but never reached the Echo",
    "disabled": "shown disabled",
    "missing":  "in the source, not on the page",
    "FAIL":     "did not do what it says",
}
ORDER = ["FAIL", "api", "missing", "disabled", "received", "applied"]


def build(version: str, controls: dict, a11y: dict, human: dict | None) -> str:
    rows = controls["controls"]
    n = Counter(r["result"] for r in rows)
    fails = [r for r in rows if r["result"] in ("FAIL", "api", "missing")]
    views = a11y["views"]
    dirty = {v: i for v, i in views.items() if i}
    out = [f"# UAT — {version}", ""]

    verdict = "PASS" if not fails and not dirty else "ISSUES"
    out += [f"**{verdict}.** {len(rows)} dashboard controls checked on a real Echo: "
            + ", ".join(f"{n[k]} {k}" for k in ORDER if n[k]) + ". "
            + (f"Accessibility: {len(views)} views, all clean (WCAG 2.1 AA, automated)."
               if not dirty else f"Accessibility: {len(dirty)} of {len(views)} views with issues."),
            ""]
    if human:
        hn = Counter(s["outcome"] for s in human["steps"])
        out += [f"Guided steps with a person: " + ", ".join(f"{v} {k}" for k, v in hn.items()), ""]

    out += ["## How this was run", "",
            f"- Controller image `{controls.get('image')}`, isolated rig "
            "(tools/uat/rig.py); Echo `{}` attached over USB.".format(controls.get("serial")),
            f"- Controls run {controls.get('at')}; accessibility scan {a11y.get('at')}.",
            "- Each control: changed in the browser, saved, read back from the API, "
            "then checked on the Echo (`/tmp/em-config.json`: received, and applied "
            "where its running config covers the key), then put back and checked again.",
            ""]

    if fails:
        out += ["## Needs attention", ""] + [
            f"- **{r['label']}** (`{r['key']}`): {r['result']} — {r['detail']}" for r in fails] + [""]

    out += ["## Controls", "", "| Control | Key | Result | Seen |", "|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (ORDER.index(r["result"]) if r["result"] in ORDER else 0,
                                         r["line"])):
        out.append(f"| {r['label']} | `{r['key']}` | {r['result']} | {r['detail'].replace('|', '/')} |")
    out += ["", "Results: " + "; ".join(f"**{k}** {v}" for k, v in MEANING.items()), ""]

    out += ["## Accessibility", ""]
    for v, issues in views.items():
        out.append(f"- `{v}`: " + (", ".join(f"{i['id']} ({i.get('nodes', '?')})" for i in issues)
                                  if issues else "clean"))
    out += ["", "axe-core finds what a machine can. A clean scan is a floor, "
                "not a screen-reader test.", ""]

    out += ["## Actions not checked automatically", "",
            "Every state-changing call the dashboard makes. These are covered by "
            "the guided steps below, or not at all:", ""]
    out += [f"- `{a['method']} {a['path']}`" for a in inventory.actions()] + [""]

    if human:
        out += ["## Guided steps (with a person)", "", "| Step | Outcome | Notes |", "|---|---|---|"]
        out += [f"| {s['id']} {s['title']} | {s['outcome']} | {s.get('notes', '')} |"
                for s in human["steps"]]
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--controls", required=True)
    ap.add_argument("--a11y", required=True)
    ap.add_argument("--human")
    a = ap.parse_args()
    md = build(a.version, json.load(open(a.controls)), json.load(open(a.a11y)),
               json.load(open(a.human)) if a.human else None)
    dest = ROOT / "docs" / "uat-results" / f"{a.version}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(md)
    print(dest)
