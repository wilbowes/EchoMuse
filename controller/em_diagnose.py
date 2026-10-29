"""
Findings from a support bundle: what is wrong, or likely wrong, and the
numbers behind it.

Pure over the bundle's JSON (em_support.build), so it runs the same on a file
attached to an issue (tools/bundle_check.py) as on one built in-process, and
the tests drive it from fixtures with no database or Echo.

Each finding is one sentence plus its evidence, at one of two levels:

- "fact": the bundle states it outright (HA is not connected; the Echo
  reports it cannot run its wake word; a setting needs a capability this
  firmware does not announce).
- "suggests": a rate or count past a threshold. The thresholds are starting
  values, to be checked against a healthy fleet's bundle before they are
  trusted; a rule that fires on a healthy fleet is noise.

Rules reuse the modules that decide the same thing for the dashboard
(em_health for boots and wear), so a finding cannot disagree with the panel.
Nothing is concluded from absence: a log line missing from the window is not
evidence that it did not happen.
"""

from __future__ import annotations

from typing import Any, Callable

import em_db
import em_health

FACT = "fact"
SUGGESTS = "suggests"

# Settings that do nothing on firmware without the capability, as the
# dashboard disables them. Only keys whose effect is entirely on the Echo:
# EQ and the limiter are applied by the controller instead when a device
# lacks output_chain, so they are not listed.
NEEDS_CAPABILITY: dict[str, str] = {
    "wakeSound": "wake_cue",
    "wakeSoundLevel": "wake_cue",
    "volumeButtonSound": "volume_cue",
    "wakeMic": "wake_mic",
    "remoteVolumeArc": "remote_volume_arc",
    "responseLevel": "response_level",
    "sendspinEnabled": "sendspin",
    "sendspinUnpaired": "sendspin",
    "bleProxyConnections": "ble_connect",
    "aecRefSource": "aec_hw_ref",
    "buttonSingleTapEvent": "button_hold",
}

# Turn outcomes that mean the person got no answer.
FAILED_OUTCOMES = frozenset({
    "no_ha", "tts_error", "no_tts", "timeout", "stream_timeout",
    "pipeline_refused", "silent_reply",
})

MIN_TURNS = 5             # below this a rate says nothing
NO_SPEECH_RATE = 0.30     # fleet ran ~15% in Sep (false-wake baseline)
FAILED_RATE = 0.20
UNDERRUN_RATE = 0.20
NEAR_MISSES = 10          # per bundle window
DISK_FREE_MB = 500
MEM_AVAILABLE_FRACTION = 0.10
CPU_5M_PCT = 90


def _finding(where: str, rule: str, level: str, text: str, **evidence) -> dict:
    return {"device": where, "rule": rule, "level": level, "text": text,
            "evidence": evidence}


# ─── Per device ──────────────────────────────────────────────────────────────

def _offline(d: dict, ctx: dict) -> list[dict]:
    if d.get("live", {}).get("connected", True):
        return []
    return [_finding(d["name"], "offline", FACT,
                     "not connected; everything live below is unknown",
                     last_seen=d.get("last_seen"))]


def _ha_not_connected(d: dict, ctx: dict) -> list[dict]:
    view = d.get("view") or {}
    if not d.get("live", {}).get("connected") or "voiceSatellite" not in view:
        return []
    sat = view["voiceSatellite"]
    if sat and sat.get("haConnected"):
        return []
    return [_finding(d["name"], "ha_not_connected", FACT,
                     "Home Assistant is not connected to its voice satellite, "
                     "so a wake lights the ring and nothing answers",
                     satellite=sat)]


def _listening(d: dict, ctx: dict) -> list[dict]:
    listen = (d.get("view") or {}).get("listen")
    if not listen or listen.get("state") not in ("degraded", "legacy"):
        return []
    text = {"degraded": "cannot run its own wake word (button only)",
            "legacy": "streams continuously: firmware predates private listening"}
    return [_finding(d["name"], f"listen_{listen['state']}", FACT,
                     text[listen["state"]], reason=listen.get("reason"))]


def _link(d: dict, ctx: dict) -> list[dict]:
    view = d.get("view") or {}
    out = []
    if view.get("linkRefused"):
        out.append(_finding(d["name"], "link_refused", FACT,
                            "the controller is refusing its connection",
                            reason=view["linkRefused"]))
    quality = view.get("linkQuality") or {}
    if quality.get("verdict") == "poor":
        out.append(_finding(d["name"], "link_poor", FACT,
                            "packet loss over the last 10 minutes is high enough "
                            "to be heard", loss_pct=quality.get("lossPct")))
    return out


def _settings_ignored(d: dict, ctx: dict) -> list[dict]:
    live = d.get("live", {})
    if not live.get("connected"):
        return []
    caps = set(live.get("capabilities") or ())
    cfg = d.get("config") or {}
    defaults = em_db.DEFAULT_DEVICE_CONFIG
    out = []
    for key, cap in NEEDS_CAPABILITY.items():
        if cap in caps or key not in cfg or cfg[key] == defaults.get(key):
            continue
        out.append(_finding(d["name"], "setting_ignored", FACT,
                            f"{key} is set but this firmware has no {cap}, "
                            "so it does nothing", key=key, value=cfg[key],
                            capability=cap))
    return out


def _updates(d: dict, ctx: dict) -> list[dict]:
    view = d.get("view") or {}
    out = []
    for key, what in (("update_error", "firmware"), ("emos_update_error", "emOS")):
        if view.get(key):
            out.append(_finding(d["name"], f"{key.removesuffix('_error')}_failed",
                                FACT, f"the last {what} update failed",
                                error=view[key]))
    if view.get("firmware_update"):
        out.append(_finding(d["name"], "firmware_behind", FACT,
                            "runs older firmware than this controller offers",
                            firmware=d.get("firmware_ver")))
    last = ((view.get("wifiChange") or {}).get("last") or {})
    if last.get("ok") is False:
        out.append(_finding(d["name"], "wifi_change_failed", FACT,
                            "its last WiFi change failed", error=last.get("error")))
    return out


def _hardware(d: dict, ctx: dict) -> list[dict]:
    boot = d.get("live", {}).get("boot") or {}
    out = []
    summary = em_health.boot_summary(boot.get("boot_reason"))
    if summary and summary["level"] == "warn":
        out.append(_finding(d["name"], "unplanned_boot", FACT,
                            "its last boot was unplanned", reason=summary["text"]))
    wear = em_health.emmc_summary(boot)
    if wear and wear["level"] in ("warn", "error"):
        out.append(_finding(d["name"], "emmc_wear", FACT,
                            "its flash is near or past rated life", wear=wear["text"]))
    hot = [m for m in ctx["metrics"].get(d.get("device_id"), [])
           if isinstance(m.get("cores_online_min"), int)
           and isinstance(m.get("cores_total"), int)
           and m["cores_online_min"] < m["cores_total"]]
    if hot:
        out.append(_finding(d["name"], "thermal_cores", FACT,
                            "took cores offline for heat",
                            hours=len(hot),
                            fewest=min(m["cores_online_min"] for m in hot)))
    return out


def _turns(d: dict, ctx: dict) -> list[dict]:
    turns = ctx["turns"].get(d.get("device_id"), [])
    out = []
    if len(turns) >= MIN_TURNS:
        failed = [t for t in turns if t.get("outcome") in FAILED_OUTCOMES]
        if len(failed) / len(turns) >= FAILED_RATE:
            by = {}
            for t in failed:
                by[t["outcome"]] = by.get(t["outcome"], 0) + 1
            out.append(_finding(d["name"], "turns_failing", SUGGESTS,
                                f"{len(failed)} of {len(turns)} turns got no answer",
                                outcomes=by))
        late = [t for t in turns if (t.get("underruns") or 0) > 0]
        if len(late) / len(turns) >= UNDERRUN_RATE:
            out.append(_finding(d["name"], "reply_underruns", SUGGESTS,
                                f"{len(late)} of {len(turns)} replies ran out of "
                                "audio while playing", turns=len(turns)))
    wakes = [t for t in turns if t.get("trigger_type") == "wakeword"]
    if len(wakes) >= MIN_TURNS:
        empty = sum(1 for t in wakes if t.get("outcome") == "no_speech")
        if empty / len(wakes) >= NO_SPEECH_RATE:
            out.append(_finding(d["name"], "false_wakes", SUGGESTS,
                                f"{empty} of {len(wakes)} wakes heard no command",
                                threshold=(d.get("config") or {}).get("owwThreshold")))
    counters = ctx["counters"].get(d.get("device_id"), [])
    near = sum(c.get("near_misses") or 0 for c in counters)
    if near >= NEAR_MISSES:
        out.append(_finding(d["name"], "near_misses", SUGGESTS,
                            f"{near} wake scores came close without crossing",
                            highest=max((c.get("near_miss_max") or 0) for c in counters),
                            threshold=(d.get("config") or {}).get("owwThreshold")))
    return out


DEVICE_RULES: tuple[Callable[[dict, dict], list[dict]], ...] = (
    _offline, _ha_not_connected, _listening, _link, _settings_ignored,
    _updates, _hardware, _turns,
)


# ─── The controller ──────────────────────────────────────────────────────────

def _controller(bundle: dict) -> list[dict]:
    stats = (bundle.get("controller") or {}).get("stats") or {}
    out = []
    free = stats.get("data_free_mb")
    if isinstance(free, (int, float)) and free < DISK_FREE_MB:
        out.append(_finding("controller", "disk_low", FACT,
                            "its data disk is nearly full; recordings and "
                            "database writes stop when it fills", free_mb=free))
    avail, total = stats.get("mem_available_mb"), stats.get("mem_total_mb")
    if (isinstance(avail, (int, float)) and isinstance(total, (int, float))
            and total > 0 and avail / total < MEM_AVAILABLE_FRACTION):
        out.append(_finding("controller", "memory_low", FACT,
                            "the host is short of memory",
                            available_mb=avail, total_mb=total))
    cpu = stats.get("cpu_pct_5m")
    if isinstance(cpu, (int, float)) and cpu >= CPU_5M_PCT:
        out.append(_finding("controller", "cpu_busy", SUGGESTS,
                            "the controller was busy over the last 5 minutes",
                            cpu_pct_5m=cpu, cpu_pct_1h=stats.get("cpu_pct_1h")))
    return out


def _by_device(rows: list[Any]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows or ():
        if isinstance(r, dict):
            out.setdefault(r.get("device_id"), []).append(r)
    return out


def check(bundle: dict) -> list[dict]:
    """Every finding, facts before suggestions, controller first."""
    ctx = {"turns": _by_device(bundle.get("turns")),
           "metrics": _by_device(bundle.get("metrics")),
           "counters": _by_device(bundle.get("wake_counters"))}
    found = _controller(bundle)
    for d in bundle.get("devices") or ():
        if not d.get("approved", True):
            continue
        for rule in DEVICE_RULES:
            found += rule(d, ctx)
    return sorted(found, key=lambda f: f["level"] != FACT)
