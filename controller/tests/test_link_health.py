"""
Whether a device's link is unhealthy, from the fleet's own recorded hours.

#301: recovery that works silently means a link degrading over weeks looks
like nothing at all until the day it stops working. The measurements were
already being recorded — `device_metrics` keeps an hourly row per device with
the idle control-plane RTT excursion counts and the hour's worst — and what
this pins is the decision over them: which device, how bad, and against what.

Nothing here needs a fleet, a device or a database. `FLEET` is the rest of
the fleet's recorded history, built to a shape whose percentiles are stated
here (10% and 19%) rather than recomputed by the code under test.
"""

import _sources
import _oww_stub

import em_link_health as LH

_oww_stub.install()
try:
    import em_controller          # RTT_EXCURSION_MS, the threshold the counts were made against
finally:
    _oww_stub.uninstall()

HOUR = 3600
NOW  = 1_800_000_000              # 500000 * 3600, a whole hour: ages are exact


def row(device_id, hours_ago, exc, samples, worst=None):
    """One hourly rollup, as db.get_link_rollups hands them over."""
    return {"device_id":           device_id,
            "hour_ts":             NOW - hours_ago * HOUR,
            "rtt_excursions_idle": exc,
            "rtt_samples_idle":    samples,
            "rtt_max_ms":          worst}


def hours(device_id, ages, rates, samples=1000, worst=None):
    """`rates` (percent of probes over the bar) for one device, one per age."""
    return [row(device_id, age, round(r * samples / 100), samples, worst)
            for age, r in zip(ages, rates)]


# The rest of the fleet: eleven recorded hours, because 0.9 * 10 is a whole
# index — the 90th percentile lands on the tenth value (10%) and the 99th
# interpolates 90% of the way to the last (19%). A device is judged against
# these, so they are the bar in every test that is not about where the bar
# comes from.
FLEET = hours("study", range(1, 12), (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20))


def verdict(device_rows, now=NOW, device_id="bedroom"):
    return LH.verdicts(FLEET + device_rows, now)[device_id]


# ─── Where the bar comes from ─────────────────────────────────────────────────

def test_the_bar_is_the_fleets_own_recorded_hours():
    """
    The threshold is not a number typed into the module: it is this fleet's
    top decile, so the verdict says "worse than what this network normally
    does" and a network that is uniformly bad says less.
    """
    v = verdict(hours("bedroom", (1, 2, 3), (12, 12, 1)))
    assert v["level"] == "warn"
    assert "2 of the last 6 hours above the 10% bar" in v["note"], v["note"]


def test_a_fleet_with_a_worse_normal_is_quieter():
    """9% hours are below this fleet's own 90th percentile, so: nothing."""
    assert verdict(hours("bedroom", (1, 2, 3, 4, 5, 6), (9,) * 6))["level"] == "ok"


def test_a_device_cannot_hide_behind_its_own_bad_hours():
    """
    Leave-one-out: with the bad device inside the distribution it is judged
    against, three devices (40%, 1%, 1%) put the 90th percentile at 40% and
    the verdict says nothing — the one case that must never be quiet.
    """
    rows = (hours("bedroom", (1, 2, 3, 4, 5, 6), (40,) * 6)
            + hours("study",   (1, 2, 3, 4, 5, 6), (1,) * 6)
            + hours("kitchen", (1, 2, 3, 4, 5, 6), (1,) * 6))
    v = LH.verdicts(rows, NOW)["bedroom"]
    assert v["level"] == "error"
    assert "1% bar" in v["note"], v["note"]


def test_one_echo_is_judged_against_its_own_normal():
    """
    A fleet of one has no other device to read a bar from, so its own history
    is the bar and the verdict can only mean "worse than this device's usual".
    Three recent hours at 30% against thirty recorded at 1%.
    """
    own = (hours("bedroom", range(11, 41), (1,) * 30)
           + hours("bedroom", (1, 2, 3), (30, 30, 30)))
    assert LH.verdicts(own, NOW)["bedroom"]["level"] == "warn"


# ─── Hysteresis and the minimum observation window ────────────────────────────

def test_one_bad_hour_is_a_stall_not_a_verdict():
    assert verdict(hours("bedroom", (1, 2, 3), (15, 1, 1)))["level"] == "ok"


def test_sitting_exactly_on_the_bar_is_not_above_it():
    """Above the bar, not at it: the bar is the top decile, and the decile
    itself is the fleet's normal."""
    assert verdict(hours("bedroom", (1, 2, 3, 4, 5, 6), (10,) * 6))["level"] == "ok"


def test_two_hours_above_the_bar_are_a_verdict():
    assert verdict(hours("bedroom", (1, 2, 3), (12, 12, 1)))["level"] == "warn"


def test_a_run_outside_anything_the_fleet_records_is_an_error():
    v = verdict(hours("bedroom", (1, 2, 3), (25, 25, 25)))
    assert v["level"] == "error"
    assert "19% bar" in v["note"], v["note"]


def test_a_device_up_for_a_few_minutes_has_a_sample_not_a_health():
    v = verdict(hours("bedroom", (1, 2), (12, 12)))
    assert v["level"] is None
    assert v["text"] == "Not enough history yet"
    assert "2 of the last 6 hours measured" in v["note"], v["note"]


def test_an_hour_with_too_few_probes_is_not_an_observation():
    """
    A row exists as soon as one stats report lands, so an hour can hold six
    probes. That is not a measurement of the hour and must not become a clean
    one — the device has no verdict yet, not a healthy one.
    """
    v = verdict(hours("bedroom", (1, 2, 3, 4, 5, 6), (0,) * 6, samples=10))
    assert v["level"] is None
    assert v["text"] == "Not enough history yet"
    assert "no hour yet with 60 idle probes" in v["note"], v["note"]


def test_a_verdict_about_hours_that_stopped_arriving_is_not_about_now():
    v = verdict(hours("bedroom", (8, 7, 6, 5), (12,) * 4))
    assert v["level"] is None
    assert v["text"] == "Not measured recently"
    assert "5 h ago" in v["note"], v["note"]


def test_the_silence_is_reported_in_the_unit_it_lands_in():
    """Days once it is a day: "26 h ago" is a number to do arithmetic on. And
    the formatter's other two units, which the verdict reaches only as its age
    check moves."""
    v = verdict(hours("bedroom", (26, 27, 28), (12,) * 3))
    assert v["level"] is None
    assert v["note"].endswith("1 d ago"), v["note"]
    assert LH._ago(300) == "5 min ago"
    assert LH._ago(5 * HOUR) == "5 h ago"


def test_the_flag_falls_when_the_bad_hours_age_out():
    """
    The window is the hysteresis, and there is nothing stored: the same rows
    read three hours earlier warned, and read now they are clean, because the
    bad hours have left the window. That is what "clears itself" means for an
    indicator nobody has to refresh.
    """
    rows = (hours("bedroom", (7, 8), (15, 15))
            + hours("bedroom", (1, 2, 3, 4, 5, 6), (1,) * 6))
    assert LH.verdicts(FLEET + rows, NOW - 3 * HOUR)["bedroom"]["level"] == "warn"
    assert LH.verdicts(FLEET + rows, NOW)["bedroom"]["level"] == "ok"


# ─── Absence is not zero ──────────────────────────────────────────────────────

def test_an_hour_the_device_did_not_measure_is_not_a_clean_hour():
    """
    NULL and 0 are different facts. A row with a missing numerator or
    denominator cannot be a zero-excursion hour, and six of them are no
    verdict rather than a clean one.
    """
    for exc, samples in ((None, 1000), (0, None), (None, None)):
        rows = [row("bedroom", age, exc, samples) for age in range(1, 7)]
        assert verdict(rows)["level"] is None, (exc, samples)


# ─── The sentence ─────────────────────────────────────────────────────────────

def test_the_sentence_says_what_to_do_about_it():
    """"Bedroom: unhealthy" is not actionable; the rate, the stall and the
    bar are, and the worst is in seconds once it is one."""
    v = verdict(hours("bedroom", (1, 2, 3), (12, 12, 12), worst=4800))
    assert v["level"] == "warn"
    assert v["text"] == "12% of pings delayed over 200 ms, worst 4.8 s"
    assert v["ratePct"] == 12.0


def test_an_hour_with_no_worst_reads_without_one():
    """rtt_max_ms is NULL until a probe lands; the sentence drops the clause
    rather than claiming a worst of 0 ms."""
    assert verdict(hours("bedroom", (1, 2, 3), (12, 12, 12)))["text"] \
        == "12% of pings delayed over 200 ms"
    assert ", worst 812 ms" in verdict(
        hours("bedroom", (1, 2, 3), (12, 12, 12), worst=812))["text"]


def test_the_rate_is_pooled_over_the_hours_not_a_mean_of_rates():
    """
    "What fraction of pings were late" is a pooled question: a 900-probe hour
    weighs nine times a 100-probe one, where a mean of the six hourly rates
    would give the quiet hour the same weight as the loud one.
    """
    rows = [row("bedroom", 1, 1, 100), row("bedroom", 2, 180, 900),
            row("bedroom", 3, 1, 100)]
    assert verdict(rows)["text"].startswith("16.5% of pings"), verdict(rows)["text"]


def test_the_bar_is_under_the_worst_hour_from_the_first_day():
    """
    Interpolation, not nearest-rank: with three recorded hours the largest
    value IS the 99th percentile under nearest-rank, so nothing could ever be
    above it and a brand-new install would read healthy until it had enough
    history to be wrong about.
    """
    assert LH._percentile([1.0, 2.0, 3.0], LH.BAD_PCTL) < 3.0
    assert LH._percentile([1.0, 2.0, 3.0], LH.WARN_PCTL) < 3.0
    assert LH._percentile([], LH.WARN_PCTL) is None


def test_a_rate_keeps_its_decimal_only_when_it_has_one():
    assert LH._pct(12.0) == "12"
    assert LH._pct(3.2) == "3.2"


# ─── The counter and the sentence agree on what an excursion is ───────────────

def test_the_excursion_threshold_is_the_one_the_counts_were_made_against():
    """
    em_link_health names RTT_EXCURSION_MS again instead of importing it (a
    pure module cannot pull in the voice pipeline), so the sentence is held
    to the constant the device's counts were actually taken against.
    """
    assert LH.EXCURSION_MS == em_controller.RTT_EXCURSION_MS


# ─── Every device gets an entry ───────────────────────────────────────────────

def test_every_device_named_in_the_rows_gets_a_verdict():
    out = LH.verdicts(
        FLEET + hours("bedroom", (1, 2), (12, 12))
        + [row("hall", 1, 0, 10)], NOW)
    assert set(out) == {"study", "bedroom", "hall"}
    assert out["bedroom"]["level"] is None      # two hours is a sample
    assert out["hall"]["level"] is None         # ten probes is not an hour
    assert out["study"]["level"] == "ok"
    assert out["study"]["text"] == "3% of pings delayed over 200 ms"
    assert LH.verdicts([], NOW) == {}


# ─── The push that lets the flag clear itself (#301) ─────────────────────────
#
# The design on #301 is explicit that the flag and the push are one operation:
# an indicator that needs a page refresh to go away teaches people to distrust
# it. `changed` is the whole of that decision — which devices a dashboard has
# to be told about between two reads — so it is pinned here rather than in the
# loop that cannot be imported.

def test_only_devices_whose_verdict_moved_are_pushed():
    before = {"bedroom": {"level": "ok"}, "study": {"level": "warn"}}
    after  = {"study": {"level": "warn"}, "hall": {"level": "ok"}}
    assert LH.changed(before, after) == [("bedroom", None),
                                         ("hall", {"level": "ok"})]


def test_a_new_level_under_the_old_sentence_is_a_change():
    before = {"bedroom": {"level": "warn",  "text": "12% of pings delayed"}}
    after  = {"bedroom": {"level": "error", "text": "12% of pings delayed"}}
    assert LH.changed(before, after) == [("bedroom", after["bedroom"])]


def test_a_device_that_no_longer_has_a_verdict_is_pushed_as_none():
    # Hours aging out of the window is the ordinary way a warning clears.
    assert LH.changed({"bedroom": {"level": "error"}}, {}) == [("bedroom", None)]


def test_nothing_moving_pushes_nothing():
    same = {"bedroom": {"level": "warn", "text": "12% of pings delayed"}}
    assert LH.changed(same, dict(same)) == []
    assert LH.changed({}, {}) == []


def test_the_loop_pushes_the_verdict_it_read():
    """
    The two lines that make the flag self-clearing: the loop reads the same
    link_health the API serves, and every move reaches the same push channel
    the device state uses. A guard, not a call — em_controller cannot be
    imported by the job that runs this without the openwakeword stub.
    """
    body = _sources.code_only(_sources.def_source("_link_verdict_loop"))
    assert "em_link_health.changed(" in body
    assert "linkHealth" in body
    assert "em_events.push(" in body
