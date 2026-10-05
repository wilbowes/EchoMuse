"""
Correlating on-device wake crossings with the controller's own detections.

The arithmetic here is small but the failure modes are quiet: a match window
that is too tight records working agreement as a miss, and a match that is not
consumed lets one crossing vouch for two turns. Both produce a plausible
report, which is why they are tested rather than eyeballed.
"""

import pytest

import em_shadow


def tracker():
    return em_shadow.ShadowTracker()


def test_match_returns_score_and_signed_delta():
    t = tracker()
    t.active = True
    # Device crossed 120ms BEFORE the controller — the expected direction, since
    # the device scores the frame it just captured and the controller scores the
    # same frame after a network hop.
    t.record_cross(0.83, age_ms=0, at_now=1000.0)
    score, delta = t.match(wake_mono=1000.12)

    assert score == 0.83
    assert delta == -120, "device-first must read as a negative delta"


def test_age_is_projected_backwards_onto_the_report():
    """The device reports how long AGO it crossed, because its wall clock is
    not trustworthy before NTP. A report that took 200ms to arrive describes a
    crossing 200ms before it landed, not at arrival."""
    t = tracker()
    t.record_cross(0.7, age_ms=200, at_now=500.0)
    score, delta = t.match(wake_mono=499.8)
    assert score == 0.7
    assert delta == 0, "a 200ms-old report at t=500 describes a crossing at 499.8"


def test_no_crossing_in_window_is_a_miss_not_an_error():
    t = tracker()
    t.record_cross(0.9, age_ms=0, at_now=100.0)
    # Far outside the window: a different utterance entirely.
    score, delta = t.match(wake_mono=100.0 + em_shadow.MATCH_WINDOW_S + 1.0)
    assert (score, delta) == (None, None)


def test_boundary_of_the_match_window():
    t = tracker()
    t.record_cross(0.6, age_ms=0, at_now=0.0)
    just_in = em_shadow.MATCH_WINDOW_S - 0.01
    assert t.match(wake_mono=just_in)[0] == 0.6

    t.record_cross(0.6, age_ms=0, at_now=0.0)
    just_out = em_shadow.MATCH_WINDOW_S + 0.01
    assert t.match(wake_mono=just_out)[0] is None


def test_nearest_crossing_wins():
    t = tracker()
    t.record_cross(0.5, age_ms=0, at_now=10.0)   # 1.0s before the wake
    t.record_cross(0.95, age_ms=0, at_now=10.9)  # 0.1s before the wake
    score, delta = t.match(wake_mono=11.0)
    assert score == 0.95, "the nearer crossing describes this utterance"
    assert delta == -100


def test_a_match_is_consumed_so_two_turns_cannot_share_it():
    """Two turns in quick succession must not both be credited to one crossing —
    that would report agreement on a turn the device never detected."""
    t = tracker()
    t.record_cross(0.88, age_ms=0, at_now=50.0)

    first = t.match(wake_mono=50.05)
    second = t.match(wake_mono=50.10)

    assert first[0] == 0.88
    assert second == (None, None)
    assert t.pending() == 0


def test_only_the_matched_crossing_is_consumed():
    t = tracker()
    t.record_cross(0.4, age_ms=0, at_now=0.0)
    t.record_cross(0.9, age_ms=0, at_now=100.0)
    assert t.match(wake_mono=100.0)[0] == 0.9
    # The unrelated one is still available for its own turn.
    assert t.pending() == 1
    assert t.match(wake_mono=0.0)[0] == 0.4


def test_absurd_age_is_clamped_not_trusted():
    """A stale or malformed age must not be projected onto an unrelated wake."""
    t = tracker()
    t.record_cross(0.9, age_ms=600_000, at_now=1000.0)  # claims 10 minutes ago
    # Clamped to MAX_AGE_S, so it lands there and not 600s back.
    score, _ = t.match(wake_mono=1000.0 - em_shadow.MAX_AGE_S)
    assert score == 0.9


def test_malformed_report_is_dropped_not_coerced():
    """Inventing a 0.0 score for an unparseable report would silently become a
    data point arguing the device detected nothing."""
    t = tracker()
    assert t.record_cross("not-a-number", age_ms=0) is False
    assert t.record_cross(None, age_ms=0) is False
    assert t.pending() == 0


def test_missing_age_is_treated_as_now():
    t = tracker()
    assert t.record_cross(0.75, age_ms=None, at_now=42.0) is True
    assert t.match(wake_mono=42.0) == (0.75, 0)


def test_ring_is_bounded():
    """Crossings arrive without any turn to consume them (false accepts in an
    empty room), so the ring must not grow without bound."""
    t = em_shadow.ShadowTracker(maxlen=4)
    for i in range(20):
        t.record_cross(0.9, age_ms=0, at_now=float(i))
    assert t.pending() == 4


def test_active_defaults_false():
    """A device is not assumed to be scoring: `active` is what stops a missing
    per-turn score from being read as a miss."""
    assert tracker().active is False


# ─── owwOnDevice modes ───────────────────────────────────────────────────────
#
# The mode decides who is allowed to start a turn, and both wrong answers are
# expensive: honouring "on" against firmware that cannot trigger leaves a
# device deaf, and acting on our own score in "on" mode answers twice.


def test_unknown_mode_is_off_not_guessed():
    """The two plausible readings of an unknown mode are 'score silently' and
    'start triggering', and one of those is a live behaviour change."""
    for v in ("triggering", "ON DEVICE", "", None, 1, "yes"):
        assert em_shadow.normalise_mode(v) == em_shadow.MODE_OFF


def test_known_modes_survive_case_and_whitespace():
    assert em_shadow.normalise_mode("  On ") == em_shadow.MODE_ON
    assert em_shadow.normalise_mode("SHADOW") == em_shadow.MODE_SHADOW


def test_on_without_the_capability_degrades_to_shadow_not_on():
    """Honouring it would stop the controller acting on its own detections
    while waiting for wakes the firmware has no code to send — a deaf device,
    which is a wrong answer rather than the old behaviour."""
    assert em_shadow.effective_mode("on", trigger_capable=False) == em_shadow.MODE_SHADOW
    assert em_shadow.effective_mode("on", trigger_capable=True) == em_shadow.MODE_ON


def test_capability_does_not_promote_a_lesser_mode():
    for mode in ("off", "shadow"):
        assert em_shadow.effective_mode(mode, trigger_capable=True) == mode


# ── a missing classifier must not deafen the device (#191) ───────────────────


def test_a_missing_model_never_changes_the_mode():
    """
    It used to drop "on" to "off" so the controller would trigger instead.
    Wil, 2026-09-22: a device configured for its own wake word is never moved
    to the controller's — it answers the button until the connect reconcile
    installs the model. So readiness is not an input to the mode at all.
    """
    import inspect
    params = inspect.signature(em_shadow.effective_mode).parameters
    assert not [p for p in params if "model" in p or "ready" in p or "fallback" in p]
    assert em_shadow.effective_mode("on", trigger_capable=True) == em_shadow.MODE_ON


def test_off_stays_off_whatever_the_capability():
    for capable in (True, False):
        assert em_shadow.effective_mode("off", trigger_capable=capable) == em_shadow.MODE_OFF


def test_in_on_mode_only_the_device_triggers():
    """The controller keeps scoring — that is the comparison — but its own
    crossing must not start a turn, or every utterance is answered twice."""
    wake = {"at": 0.0, "score": 0.9, "threshold": 0.5}
    assert em_shadow.decide_wake_source("on", wake, ctrl_hit=False) == "device"
    assert em_shadow.decide_wake_source("on", wake, ctrl_hit=True) == "device"
    assert em_shadow.decide_wake_source("on", None, ctrl_hit=True) == "none"
    assert em_shadow.decide_wake_source("on", None, ctrl_hit=False) == "none"


def test_off_and_shadow_ignore_a_device_wake_entirely():
    """A device that keeps triggering after the mode is turned down must not
    be obeyed: the controller's view of the mode is the one shown to the user."""
    wake = {"at": 0.0, "score": 0.9, "threshold": 0.5}
    for mode in ("off", "shadow", "nonsense"):
        assert em_shadow.decide_wake_source(mode, wake, ctrl_hit=False) == "none"
        assert em_shadow.decide_wake_source(mode, wake, ctrl_hit=True) == "controller"


# ─── PendingWake ─────────────────────────────────────────────────────────────


def pending():
    return em_shadow.PendingWake()


def test_wake_carries_its_corrected_instant_not_arrival():
    """The age is measured on the device, so a slow link shows up as an old
    wake — not a fresh one that then drags the comparison by the network hop."""
    p = pending()
    assert p.offer(0.82, 0.5, age_ms=250, at_now=100.0) is True
    wake, age = p.take(at_now=100.0)
    assert wake["at"] == 99.75
    assert wake["score"] == 0.82
    assert wake["threshold"] == 0.5
    assert age == 0.25


def test_stale_wake_is_dropped_and_reports_its_age():
    """A wake several seconds old means the person has finished speaking, so
    acting on it answers into silence. The age is the only evidence of why."""
    p = pending()
    p.offer(0.9, 0.5, age_ms=0, at_now=0.0)
    wake, age = p.take(at_now=em_shadow.MAX_PENDING_WAKE_S + 0.5)
    assert wake is None
    assert age == pytest.approx(em_shadow.MAX_PENDING_WAKE_S + 0.5)


def test_expiry_clears_the_slot():
    """A wake too stale to act on now will not be fresher next frame; leaving
    it would fire a turn at some arbitrary later moment."""
    p = pending()
    p.offer(0.9, 0.5, age_ms=0, at_now=0.0)
    p.take(at_now=100.0)
    assert p.peek() is False
    assert p.take(at_now=100.0) == (None, None)


def test_take_consumes_so_one_wake_cannot_start_two_turns():
    p = pending()
    p.offer(0.9, 0.5, age_ms=0, at_now=0.0)
    assert p.take(at_now=0.0)[0] is not None
    assert p.take(at_now=0.0) == (None, None)


def test_second_wake_replaces_the_first():
    """One utterance scored twice, or a person repeating themselves because
    nothing happened — both want one turn on the newest wake, not a queue that
    starts a second turn the moment the first ends."""
    p = pending()
    p.offer(0.7, 0.5, age_ms=0, at_now=0.0)
    p.offer(0.95, 0.5, age_ms=0, at_now=1.0)
    wake, _ = p.take(at_now=1.0)
    assert wake["score"] == 0.95
    assert p.peek() is False


def test_malformed_wake_is_dropped_not_coerced():
    """A turn is a worse thing to invent than a data point."""
    p = pending()
    assert p.offer("loud", 0.5, age_ms=0) is False
    assert p.peek() is False


def test_missing_threshold_does_not_cost_the_wake():
    """The threshold is recorded against the turn, not used to decide
    anything — losing it must not lose the user their wake."""
    p = pending()
    assert p.offer(0.9, None, age_ms=0, at_now=0.0) is True
    wake, _ = p.take(at_now=0.0)
    assert wake["score"] == 0.9
    assert wake["threshold"] is None


def test_absurd_wake_age_is_clamped_and_then_dropped():
    """Clamping bounds how far back a wild report can be projected; the
    staleness limit then refuses it, because MAX_AGE_S is well past the point
    where starting a turn is any use. Both bounds apply, in that order."""
    p = pending()
    assert p.offer(0.9, 0.5, age_ms=600_000, at_now=1000.0) is True
    wake, age = p.take(at_now=1000.0)
    assert wake is None
    assert age == pytest.approx(em_shadow.MAX_AGE_S)
