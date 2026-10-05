"""
Release UAT, part 1: does every dashboard control do what it says?

For each control in the fleet config form (inventory.py), through a real
browser against the rig's controller (rig.py up/attach):

  1. read the stored value from the API;
  2. change the control in the browser and press Save & push to fleet;
  3. the API must now hold the new value;
  4. the attached Echo must have RECEIVED it (its /tmp/em-config.json) and,
     where its running config covers the key, APPLIED it;
  5. change it back the same way, and check the Echo followed.

Each control lands in one of: applied (the Echo is running the new value),
received (the Echo got it; what it does with it needs a human or is
controller-side), api (stored but never reached the Echo), disabled (shown
disabled, with its stated reason), missing (in the source, not on the page),
or FAIL.

    python run_controls.py [--only KEY,...] [--out results.json]

Needs the Playwright client (pip install playwright==1.49.1) and the browser
server rig.py starts; no browser is installed on the host.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from playwright.sync_api import sync_playwright

import inventory
import rig

BROWSER_WS = "ws://127.0.0.1:3111/"
SETTLE_S = 25

# Config keys the Echo runs in its output chain, and the field its report
# shows each under (device/internal/outchain.Params).
OUTPUT_FIELDS = {
    "eqLoudness": "Loudness", "bassGuardEnabled": "GuardEnabled",
    "bassGuardDb": "GuardDb", "limiterEnabled": "LimiterEnabled",
    "limiterThreshold": "LimiterThresholdDb", "limiterRelease": "LimiterReleaseMs",
}

# Controls the page shows only while another is on: key -> the switch's key.
# Hidden or disabled until the other is on.
DEPENDS_ON = {"wakeSoundLevel": "wakeSound", "buttonMultiTapMs": "buttonSingleTapEvent"}


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def fleet_value(key):
    return rig.api("GET", "/api/global/config").get(key)


def wait_value(fn, want, timeout=SETTLE_S):
    end = time.time() + timeout
    got = None
    while time.time() < end:
        try:
            got = fn()
            if _same(got, want):
                return True, got
        except Exception as e:  # a serial read can time out; try again
            got = f"error: {e}"
        time.sleep(1.5)
    return False, got


class Page:
    def __init__(self, pw, state):
        self.browser = pw.chromium.connect(BROWSER_WS)
        self.page = self.browser.new_page(viewport={"width": 1400, "height": 1000})
        self.page.goto(rig.API + "/")
        self.page.fill("#li-user", state["username"])
        self.page.fill("#li-pass", state["password"])
        self.page.click("#li-btn")
        self.page.wait_for_url("**/dashboard*", timeout=20000)
        self.open_fleet_config()

    def open_fleet_config(self):
        self.page.get_by_role("button", name="Settings").click()
        self.page.get_by_role("button", name="Config", exact=True).click()
        self.page.wait_for_timeout(500)
        # Collapsed "advanced" stages: open every one so each control exists.
        for b in self.page.get_by_role("button", name="Advanced").all():
            if b.get_attribute("aria-expanded") != "true":
                b.click()

    def locate(self, c):
        role = {"Toggle": "switch", "Slider": "slider", "Select": "radiogroup",
                "NumberField": "textbox"}[c["kind"]]
        loc = self.page.get_by_role(role, name=c["label"], exact=True)
        return loc.first if loc.count() else None

    def disabled(self, c, el) -> bool:
        if c["kind"] == "Toggle":
            return el.get_attribute("aria-disabled") == "true"
        if c["kind"] == "Select":
            return all(r.is_disabled() for r in el.get_by_role("radio").all())
        return el.is_disabled()

    def change(self, c, el, current, target=None):
        """Operate the control the way a person would. Returns what it set."""
        k = c["kind"]
        if k == "Toggle":
            el.click()
            return not bool(current)
        if k == "Slider":
            # One arrow step, and back with the opposite one: the way a
            # person moves it, and right for sliders whose track is mapped
            # (Sensitivity's is mirrored, so the track is not the value).
            el.focus()
            if target is None:
                at_max = float(el.input_value()) >= float(el.get_attribute("max"))
                self.last_key = "ArrowLeft" if at_max else "ArrowRight"
                el.press(self.last_key)
            else:
                el.press({"ArrowLeft": "ArrowRight", "ArrowRight": "ArrowLeft"}[self.last_key])
            self.page.wait_for_timeout(200)
            return None
        if k == "Select":
            # Any other enabled option. Putting one BACK is revert_select's
            # job: which option stores which value is only visible after Save.
            pick = next(r for r in el.get_by_role("radio").all()
                        if r.get_attribute("aria-checked") != "true" and not r.is_disabled())
            pick.click()
            return None
        if k == "NumberField":
            v = int(current or 0)
            new = target if target is not None else (v + 1 if v < 90 else v - 1)
            el.fill(str(new))
            el.blur()
            return new

    def save(self):
        self.page.get_by_role("button", name="Save & push to fleet").click()
        # Wait for the reply before the next edit. Switching the Bluetooth
        # proxy on holds it ~1s per Echo, and an edit made meanwhile was
        # marked saved without being sent (found this way; fixed in #676).
        saving = self.page.get_by_role("button", name="Saving…")
        try:
            saving.wait_for(state="visible", timeout=1500)
        except Exception:
            pass    # already done: a fast save never shows it
        saving.wait_for(state="detached", timeout=60000)
        self.page.wait_for_timeout(1500)

    def shot(self, path):
        self.page.screenshot(path=path, full_page=True)


def run(only: set | None, out: str):
    state = rig._state()
    serials = state.get("serials") or []
    if not serials:
        raise SystemExit("no Echo attached: rig.py attach SERIAL first")
    controls_all = [c for c in inventory.controls() if c["in"] == "DeviceConfigForm"]
    controls = [c for c in controls_all
                if c["key"]
                and (not only or c["key"] in only)]
    echoes = {sn: rig.Echo(sn) for sn in serials}

    def on_device(echo, key, section="received"):
        rep = echo.config_report() or {}
        return (rep.get(section) or {}).get(key)

    def check_echo(echo, key, after):
        """One Echo's verdict on a saved value: applied, received or api."""
        ok, got = wait_value(lambda: on_device(echo, key), after)
        if not ok:
            return "api", f"received {got!r}"
        rep = echo.config_report() or {}
        applied = dict(rep.get("applied") or {})
        out_field = OUTPUT_FIELDS.get(key)
        if out_field and out_field in (rep.get("output") or {}):
            applied[key] = rep["output"][out_field]
        if key in applied:
            return ("applied" if _same(applied[key], after) else "FAIL",
                    f"running {applied[key]!r}")
        return "received", "received"

    WORST = ["FAIL", "api", "received", "applied"]

    results = []
    with sync_playwright() as pw:
        ui = Page(pw, state)
        for c in controls:
            row = {**c, "result": None, "detail": ""}
            results.append(row)
            try:
                el = ui.locate(c)
                parent = DEPENDS_ON.get(c["key"])
                if parent and (el is None or ui.disabled(c, el)):
                    pc = next(x for x in controls_all if x["key"] == parent)
                    if not fleet_value(parent):
                        ui.locate(pc).click()
                        ui.save()
                        row["restore_parent"] = pc
                    el = ui.locate(c)
                if el is None:
                    row.update(result="missing", detail="not found on the fleet Config page")
                    continue
                if ui.disabled(c, el):
                    row.update(result="disabled", detail="shown disabled")
                    continue
                before = fleet_value(c["key"])
                ui.change(c, el, before)
                ui.save()
                after = fleet_value(c["key"])
                if _same(after, before):
                    row.update(result="FAIL", detail=f"API still {before!r} after Save")
                    continue
                row["devices"] = {}
                for sn, echo in echoes.items():
                    res, det = check_echo(echo, c["key"], after)
                    row["devices"][sn] = {"result": res, "detail": det, "base": echo.base}
                row["result"] = min((d["result"] for d in row["devices"].values()),
                                    key=WORST.index)
                row["detail"] = f"{before!r}->{after!r}"
                # Put it back, the same way, and check the Echo followed.
                el = ui.locate(c)
                if c["kind"] == "Select":
                    for i in range(len(el.get_by_role("radio").all())):
                        r = ui.locate(c).get_by_role("radio").nth(i)
                        if r.get_attribute("aria-checked") == "true" or r.is_disabled():
                            continue
                        r.click()
                        ui.save()
                        if _same(fleet_value(c["key"]), before):
                            break
                else:
                    ui.change(c, el, after, target=before)
                    ui.save()
                back = fleet_value(c["key"])
                if not _same(back, before):
                    row["detail"] += f"; REVERT left API at {back!r}"
                    row["result"] = "FAIL"
                else:
                    for sn, echo in echoes.items():
                        d = row["devices"][sn]
                        if d["result"] not in ("applied", "received"):
                            continue
                        ok, got = wait_value(lambda: on_device(echo, c["key"]), back)
                        if not ok:
                            d.update(result="FAIL", detail=d["detail"] + f"; revert not received ({got!r})")
                            row["result"] = "FAIL"
            except Exception as e:
                row.update(result="FAIL", detail=f"{type(e).__name__}: {e}"[:300])
            finally:
                pc = row.pop("restore_parent", None)
                if pc:
                    try:
                        ui.locate(pc).click()
                        ui.save()
                    except Exception as e:
                        row["detail"] += f"; could not switch {pc['key']} back: {e}"
                per = " ".join(f"{sn[-4:]}={d['result']}" for sn, d in (row.get("devices") or {}).items())
                print(f"{row['result'] or '?':<9} {c['key']:<22} {row['detail']}  {per}", flush=True)
        ui.shot(out.replace(".json", ".png"))
    for echo in echoes.values():
        echo.close()
    json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "image": state.get("image"),
               "echoes": {sn: e.base for sn, e in echoes.items()}, "controls": results},
              open(out, "w"), indent=1)
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="controls.json")
    a = ap.parse_args()
    run(set(filter(None, a.only.split(","))), a.out)
