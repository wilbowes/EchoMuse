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

# Settings that only an emOS userspace acts on. FireOS ignoring them is
# the design (the dashboard says so), not a failure.
EMOS_ONLY = {"consolePassword", "consoleTimeoutMin"}


def _cell(r: dict, sn: str, base: str) -> str:
    d = (r.get("devices") or {}).get(sn)
    if not d:
        return "—"
    if base == "fireos" and r["key"] in EMOS_ONLY and d["result"] != "applied":
        return "n/a (emOS only)"
    return d["result"]


def _row_result(r: dict, echoes: dict) -> str:
    """Worst per-Echo result, not counting expected emOS-only gaps."""
    cells = [_cell(r, sn, b) for sn, b in echoes.items()]
    real = [c for c in cells if c in ORDER]
    return min(real, key=ORDER.index) if real else r["result"]


# For readers who have never met UAT: every report opens with it (Wil,
# 2026-09-27).
ABOUT = [
    "## About this report", "",
    "**What:** user acceptance testing (UAT) is the last check before a release: "
    "does the software do what it says, on real hardware, the way someone would use "
    "it. Automated tests check the code; this checks the product.", "",
    "**Why:** settings that saved and then did nothing have shipped before. Every "
    "EchoMuse release now gets this pass, and the report ships with it.", "",
    "**How:** a script drives the dashboard in a browser and changes every setting, "
    "then reads each Echo to confirm it received and applied the change, and puts it "
    "back. An accessibility scanner checks every screen. A person then works through "
    "voice, music, buttons and pairing with the Echoes in front of them, while the "
    "logs are watched.", "",
]


def build(version: str, controls: dict, a11y: dict, human: dict | None) -> str:
    rows = controls["controls"]
    echoes = controls.get("echoes") or {controls.get("serial"): "emos"}
    for r in rows:
        if r.get("devices"):
            r["result"] = _row_result(r, echoes)
    n = Counter(r["result"] for r in rows)
    fails = [r for r in rows if r["result"] in ("FAIL", "api", "missing")]
    views = a11y["views"]
    dirty = {v: i for v, i in views.items() if i}
    out = [f"# UAT — {version}", ""]
    out += ABOUT

    verdict = "PASS" if not fails and not dirty else "ISSUES"
    where = (f"{len(echoes)} real Echoes" if len(echoes) > 1 else "a real Echo")
    out += [f"**{verdict}.** {len(rows)} dashboard controls checked on {where}: "
            + ", ".join(f"{n[k]} {k}" for k in ORDER if n[k]) + ". "
            + (f"Accessibility: {len(views)} views, all clean (WCAG 2.1 AA, automated)."
               if not dirty else f"Accessibility: {len(dirty)} of {len(views)} views with issues."),
            ""]
    if human:
        hn = Counter(s["outcome"] for s in human["steps"])
        out += [f"Guided steps with a person: " + ", ".join(f"{v} {k}" for k, v in hn.items()), ""]

    out += ["## How this was run", "",
            f"- Controller image `{controls.get('image')}`, isolated rig "
            "(tools/uat/rig.py); Echoes attached over USB: "
            + ", ".join(f"`{sn}` ({b})" for sn, b in echoes.items()) + ".",
            f"- Controls run {controls.get('at')}; accessibility scan {a11y.get('at')}.",
            "- Each control: changed in the browser, saved, read back from the API, "
            "then checked on the Echo (`/tmp/em-config.json`: received, and applied "
            "where its running config covers the key), then put back and checked again.",
            ""]

    if fails:
        out += ["## Needs attention", ""] + [
            f"- **{r['label']}** (`{r['key']}`): {r['result']} — {r['detail']}" for r in fails] + [""]

    heads = [f"{sn[-4:]} ({b})" for sn, b in echoes.items()]
    out += ["## Controls", "",
            "| Control | Key | " + " | ".join(heads) + " | Seen |",
            "|---|---|" + "---|" * len(heads) + "---|"]
    for r in sorted(rows, key=lambda r: (ORDER.index(r["result"]) if r["result"] in ORDER else 0,
                                         r["line"])):
        cells = [_cell(r, sn, b) if r.get("devices") else r["result"] for sn, b in echoes.items()]
        out.append(f"| {r['label']} | `{r['key']}` | " + " | ".join(cells)
                   + f" | {r['detail'].replace('|', '/')} |")
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
