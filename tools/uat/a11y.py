"""
Release UAT: an automated accessibility scan (axe-core) of each dashboard view.

axe-core is the engine behind most WCAG checkers. It finds what a machine can
(missing names and roles, contrast, focus order problems), not everything a
screen-reader user would hit, so a clean scan is a floor rather than a pass.

    python a11y.py [--out a11y.json]
"""

from __future__ import annotations

import argparse
import json
import time

from playwright.sync_api import sync_playwright

import rig
from run_controls import BROWSER_WS

AXE = "https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js"
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]


def scan(page) -> list[dict]:
    page.add_script_tag(url=AXE)
    res = page.evaluate("t => axe.run(document, {runOnly: {type: 'tag', values: t}})", TAGS)
    return [{"id": v["id"], "impact": v["impact"], "help": v["help"],
             "nodes": len(v["nodes"]),
             "example": v["nodes"][0]["target"] if v["nodes"] else None}
            for v in res["violations"]]


def run(out: str) -> dict:
    st = rig._state()
    views = {}
    with sync_playwright() as pw:
        b = pw.chromium.connect(BROWSER_WS)
        p = b.new_page(viewport={"width": 1400, "height": 1000})
        p.goto(rig.API + "/")
        views["login"] = scan(p)
        p.fill("#li-user", st["username"]); p.fill("#li-pass", st["password"])
        p.click("#li-btn"); p.wait_for_url("**/dashboard*", timeout=20000)
        p.wait_for_timeout(1500)
        home = p.url
        views["dashboard"] = scan(p)
        if st.get("serial"):
            p.get_by_text("UAT", exact=True).first.click()
            p.wait_for_timeout(1000)
            for tab in ("status", "activity", "config", "updates", "logs"):
                try:
                    p.get_by_role("button", name=tab, exact=True).click()
                    p.wait_for_timeout(800)
                    views[f"device/{tab}"] = scan(p)
                except Exception as e:
                    views[f"device/{tab}"] = [{"id": "uat-error", "help": str(e)[:200]}]
            p.keyboard.press("Escape")
            p.goto(home); p.wait_for_timeout(1500)
        p.get_by_role("button", name="Settings").click()
        for tab in ("Config", "System", "Users", "Account", "Support"):
            p.get_by_role("button", name=tab, exact=True).click()
            p.wait_for_timeout(600)
            views[f"settings/{tab.lower()}"] = scan(p)
    json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "image": st.get("image"), "tags": TAGS, "views": views},
              open(out, "w"), indent=1)
    for v, issues in views.items():
        print(f"{v:<20} " + (", ".join(f"{i['id']}({i.get('nodes','?')})" for i in issues) or "clean"))
    return views


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="a11y.json")
    run(ap.parse_args().out)
