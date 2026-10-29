"""
Whether a device's link is unhealthy, from the fleet's own recorded rollups.

#301: recovery that works silently is the goal, and its cost is that a link
degrading over weeks looks like nothing at all until the day it stops working.
The measurements are already there — `device_metrics` holds an hourly row per
device with the idle control-plane RTT excursion counts (a probe every 5s,
`RTT_EXCURSION_MS` = 200) and the hour's worst RTT. What was missing is the
verdict, and this module is the verdict: which device, how bad, and against
what.

WHY IDLE EXCURSIONS. They are TCP retransmission delays rather than latency
(#139), and the IDLE rate is the one that means the link rather than the load.
RSSI is deliberately not an input: measured on this fleet, a device at -38dBm
threw 15.7% excursion hours while one at -71dBm threw the fewest, so signal
strength does not order the results.

WHY THERE IS NO THRESHOLD IN THIS FILE. Any number typed here would be the
value this fleet does not run. The bar is read from the fleet's own recorded
hours instead: a device-hour is UNUSUAL above the 90th percentile of every
usable hour the rest of the fleet has in the window, and BAD above the 99th.
A fleet that has only ever recorded 1% hours sets a bar near 1%; a fleet on a
bad network sets it higher and says less, which is the honest answer — health
is relative to what this network does. Two consequences worth knowing: with a
single Echo the bar is that device's own history, so it can only report
"worse than this device's normal", and a fleet that is uniformly bad reports
nothing until the history is long enough to say what its normal is.

Leave-one-out, not the whole fleet: a device measured against a distribution
it is itself dragging up reads its own degradation as normal, which is exactly
the case that must not happen — one bad device in three.

HYSTERESIS, AND A MINIMUM OBSERVATION WINDOW. A single hour above the bar is
a stall, not a verdict, and an indicator that fires on one would be ignored
for the same reason the degradation is invisible today. A device that has been
up four minutes has no health either, it has a sample. So an hour counts only
when it holds MIN_HOUR_SAMPLES idle probes; a verdict needs MIN_HOURS of those
hours in the window and MAX_AGE_HOURS of freshness; and the flag is raised
only when HOURS_ABOVE of the window's hours are above the bar. It falls the
same way — by those hours aging out of the window — so nothing is stored, and
there is no flag to drift apart from what it describes.

NOTHING LEAVES THE NETWORK, AND NOTHING REACHES THE RING. The verdict is a
sentence for the user, computed from data the controller already keeps. Per
#140 the ring is for the state recovery could not fix, and a link this module
calls unhealthy is one the dashboard has to explain, not one to light up.

Pure, over the rollup rows: the whole decision is testable without a fleet, a
device or a database. Read by em_device_view.link_health, which is what
/api/devices and the events snapshot carry as `linkHealth`.
"""

from __future__ import annotations

import time

# What a verdict describes, and how far back the fleet's own bar is read.
WINDOW_HOURS = 6
HISTORY_DAYS = 7

# An hour is an observation only when it holds this many idle probes. At one
# probe per 5s that is about five minutes of a device that was up.
MIN_HOUR_SAMPLES = 60

# Below this many usable hours in the window there is no verdict to give: a
# sample, not a health. Also the smallest set of hours the fleet's bar may be
# read from.
MIN_HOURS = 3

# Hysteresis: hours above the bar, within the window, before it is a verdict.
HOURS_ABOVE = 2

# Where "unusual" and "bad" sit in the fleet's own recorded hours. The top
# decile is worth a look; the top percentile is what a link records when it is
# failing. Two points rather than one so the two levels mean different things
# rather than the same thing louder.
WARN_PCTL = 0.90
BAD_PCTL = 0.99

# A verdict about hours that stopped arriving is not about now. Deliberately
# longer than one hour: the rollup for the current hour is written as its
# reports land, and an offline device's last hour must still be readable as
# "last measured".
MAX_AGE_HOURS = 2

# The excursion threshold the controller counts against (em_controller.
# RTT_EXCURSION_MS). Named again for the sentence rather than imported — the
# pipeline is not importable from here — and tests/test_link_health.py holds
# the two equal.
EXCURSION_MS = 200


def _percentile(sorted_vals: list, p: float) -> float | None:
    """
    Linear-interpolation percentile (numpy's "linear", the usual definition).

    Nearest-rank would be fewer lines and would put p90 ON the largest value
    for any fleet with nine or fewer usable hours — a brand-new install — so
    nothing could ever be above the bar and every device would read healthy
    until enough history existed to be wrong about. Interpolation keeps the
    bar under the worst hour from the first day.
    """
    if not sorted_vals:
        return None
    k = p * (len(sorted_vals) - 1)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _secs(ms) -> str:
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{int(ms)} ms"


def _pct(v: float) -> str:
    """A rate as text: one decimal place, and no trailing .0 — "12%", "3.2%"."""
    r = round(v, 1)
    return f"{r:.0f}" if r == int(r) else f"{r:.1f}"


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def _usable_hour(row: dict) -> dict | None:
    """
    One rollup row as an observation, or None when it is not one.

    The counts come from the device's own stats reports, so a row can exist
    with only a handful of probes behind it — that is not a measurement of
    the hour, and the two facts are kept apart by dropping it here.
    """
    samples = row.get("rtt_samples_idle")
    exc     = row.get("rtt_excursions_idle")
    if not isinstance(samples, int) or isinstance(samples, bool) \
            or samples < MIN_HOUR_SAMPLES:
        return None
    if not isinstance(exc, int) or isinstance(exc, bool) or exc < 0:
        return None
    return {
        "hour_ts":  row["hour_ts"],
        "exc":      exc,
        "samples":  samples,
        "rate":     100.0 * exc / samples,
        # The hour's worst is the worst of ALL its probes, idle or not: a 4.8s
        # stall is what someone feels, whichever state it landed in.
        "worst_ms": row.get("rtt_max_ms"),
    }


def _verdict(hours: list[dict], now: float,
             warn_bar: float, bad_bar: float) -> dict:
    """One device's hours -> {level, text, note, ratePct}."""
    now_hour = int(now // 3600) * 3600
    first    = now_hour - (WINDOW_HOURS - 1) * 3600
    recent   = [h for h in hours if first <= h["hour_ts"] <= now_hour]

    if len(recent) < MIN_HOURS:
        if not hours:
            return {"level": None, "text": "Not enough history yet",
                    "note": f"no hour yet with {MIN_HOUR_SAMPLES} idle probes",
                    "ratePct": None}
        newest = max(h["hour_ts"] for h in hours)
        if now - newest > MAX_AGE_HOURS * 3600:
            return {"level": None, "text": "Not measured recently",
                    "note": f"last measured {_ago(now - newest)}",
                    "ratePct": None}
        return {"level": None, "text": "Not enough history yet",
                "note": f"{len(recent)} of the last {WINDOW_HOURS} hours measured",
                "ratePct": None}

    samples = sum(h["samples"] for h in recent)
    rate    = 100.0 * sum(h["exc"] for h in recent) / samples
    worst   = max((h["worst_ms"] for h in recent if h["worst_ms"] is not None),
                  default=None)
    above = sum(1 for h in recent if h["rate"] > warn_bar)
    bad   = sum(1 for h in recent if h["rate"] > bad_bar)

    text = f"{_pct(rate)}% of pings delayed over {EXCURSION_MS} ms"
    if worst is not None:
        text += f", worst {_secs(worst)}"
    if bad >= HOURS_ABOVE:
        level = "error"
        note  = (f"{bad} of the last {WINDOW_HOURS} hours above the "
                 f"{_pct(bad_bar)}% bar — the worst 1% of this fleet's hours")
    elif above >= HOURS_ABOVE:
        level = "warn"
        note  = (f"{above} of the last {WINDOW_HOURS} hours above the "
                 f"{_pct(warn_bar)}% bar the rest of this fleet set")
    else:
        level = "ok"
        note  = (f"within the {_pct(warn_bar)}% bar the rest of this fleet set")
    return {"level": level, "text": text, "note": note,
            "ratePct": round(rate, 1)}


def verdicts(rows: list[dict], now: float | None = None) -> dict[str, dict]:
    """
    Every device named in `rows` -> {level, text, note, ratePct}.

    `rows` is db.get_link_rollups' output: plain dicts with device_id,
    hour_ts, rtt_excursions_idle, rtt_samples_idle and rtt_max_ms. Every
    device that appears gets an entry, including the ones with too little
    history to judge — "not measured yet" is a fact the dashboard shows, and
    a missing key would have to be guessed at.
    """
    now = time.time() if now is None else float(now)
    by_device: dict[str, list[dict]] = {}
    for row in rows:
        hours = by_device.setdefault(row["device_id"], [])
        hour = _usable_hour(row)
        if hour is not None:
            hours.append(hour)

    all_rates = sorted(h["rate"] for hours in by_device.values() for h in hours)
    out = {}
    for device_id, hours in by_device.items():
        others = sorted(h["rate"] for dev, hs in by_device.items()
                        if dev != device_id for h in hs)
        # The bar is the other devices' hours; a device judged against a
        # distribution it is itself in the tail of reads its own degradation
        # as normal. With nothing else recorded (one Echo, or a fleet that
        # just started) it falls back to its own history, which can only
        # describe what is unusual FOR IT.
        rates = others if len(others) >= MIN_HOURS else all_rates
        out[device_id] = _verdict(hours, now, _percentile(rates, WARN_PCTL),
                                  _percentile(rates, BAD_PCTL))
    return out


def changed(previous: dict, current: dict) -> list[tuple[str, dict | None]]:
    """
    Devices whose verdict moved between two reads, as (device_id, verdict).

    `verdict` is None for a device that no longer has one — a removed device,
    or hours that have all aged out of the window — and the caller pushes that
    too: an indicator with no way to clear itself is the failure the design on
    #301 names. Whole-object comparison rather than a field diff, because the
    dashboard replaces the object it holds: a new level left standing over the
    old sentence is exactly the drift a field-wise diff would let through.
    """
    out = []
    for device_id in sorted(set(previous) | set(current)):
        verdict = current.get(device_id)
        if verdict != previous.get(device_id):
            out.append((device_id, verdict))
    return out
