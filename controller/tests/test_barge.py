"""
When a wake-word score during a response counts as a barge-in, and what the
barge gives up when it cannot be had.

The first decision shipped untested and wrong, and the failure was invisible
until responses got long: the playback branch fired on ONE 80ms frame at a bar
ten times below the wake threshold, while scoring the device's own speech.
Asking for a story cut it off mid-sentence twice in a row (2026-08-20).

`cede` covers the two reasons a barge-in gives up the turn — another Echo won
the utterance, or there is no pipeline behind this one — which #414 collapsed
into one flag and only the first of which was handled the way the wake path
handles it (#417).

`FollowUpWindow` covers #195's deferral: a wake word during THINKING no longer
aborts Home Assistant's run on the frame that scores it, because that abort is
the point of no return — HA's pipeline has no resume. The person is given the
window to still be talking, and the answer in flight is kept if they are not.
"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

import em_barge

BARGE = 0.25
WAKE = 0.5


def playback(score, prev=0.0, barge=BARGE):
    return em_barge.decide(score=score, prev_score=prev, in_playback=True,
                           barge_threshold=barge, wake_threshold=WAKE)


def thinking(score, prev=0.0):
    return em_barge.decide(score=score, prev_score=prev, in_playback=False,
                           barge_threshold=BARGE, wake_threshold=WAKE)


# ── Playback: the bug ─────────────────────────────────────────────────────────

def test_one_loud_frame_during_playback_does_not_fire():
    """
    The whole bug. An isolated transient in the assistant's own narration
    scored 0.091 and 0.184 against a 0.05 bar and cancelled the response.
    """
    assert playback(0.9, prev=0.0).fired is False
    assert playback(0.184, prev=0.02, barge=0.05).fired is False


def test_two_consecutive_frames_fire():
    d = playback(0.4, prev=0.3)
    assert d.fired is True
    assert "two consecutive" in d.note


def test_a_frame_below_the_bar_breaks_the_pair():
    """Consecutive means adjacent — a dip resets the evidence."""
    assert playback(0.4, prev=BARGE - 0.01).fired is False


def test_exactly_at_the_threshold_counts():
    assert playback(BARGE, prev=BARGE).fired is True


def test_the_first_frame_of_a_run_cannot_fire():
    """
    The caller seeds prev_score at 0.0 for a fresh watcher and again after a
    stream discontinuity. A carried-over score would let one frame of a new
    turn fire on evidence from the previous one.
    """
    assert playback(0.99, prev=0.0).fired is False


def test_a_genuine_barge_still_fires_at_realistic_scores():
    """
    Speech over TTS is depressed to ~0.3-0.5 by the echo, which is what the
    0.25 default is set against. Raising the bar must not stop barge-in.
    """
    for s in (0.30, 0.42, 0.55, 0.9):
        assert playback(s, prev=s).fired is True


# ── Thinking: unchanged behaviour ─────────────────────────────────────────────

def test_one_frame_at_the_full_wake_threshold_fires_while_thinking():
    """Nothing is playing, so there is no self-echo to guard against."""
    d = thinking(WAKE)
    assert d.fired is True
    assert "two consecutive" not in d.note


def test_noise_does_not_fire_while_thinking_however_often_it_repeats():
    """
    #337. There used to be a second tier here — two consecutive frames at
    max(0.2, 0.4 * wake_threshold) — on the reasoning that someone repeating
    themselves into a silent device is a strong signal.

    Measured on a live fleet across 505 near-misses and 30 real detections:
    no genuine wake word scored below 0.502, and noise reached 0.462. The
    tier's floor of 0.2 was inside the noise band, so it could only ever
    fire on noise. It cost a real turn on 2026-08-25 — the request was
    discarded before speech recognition ran and the microphone reopened at
    the user, who asked the Echo why it was listening.
    """
    for score in (0.206, 0.238, 0.25, 0.36, 0.462):
        assert thinking(score, prev=score).fired is False, \
            f"{score} is a noise-band score and must not barge"


def test_a_person_repeating_themselves_is_still_caught():
    """
    The case the low tier existed for. Someone saying the wake word again
    scores like someone saying the wake word — the branch above catches it,
    which is why removing the tier costs nothing.
    """
    assert thinking(0.502).fired is True
    assert thinking(WAKE).fired is True

def test_a_decision_that_did_not_fire_carries_no_reason():
    """So a caller cannot log a justification for something that never fired."""
    assert playback(0.9, prev=0.0).note == ""
    assert thinking(0.0).note == ""


# ── Giving up the turn: two reasons, only one of them is an event ───────────

def cede(serves=True, won_by="test-device", device="test-device", score=0.412):
    return em_barge.cede(serves=serves, won_by=won_by, device_id=device,
                         score=score)


def test_no_ha_cedes_and_records():
    """The half the wake path already did, and the barge path did not (#417)."""
    d = cede(serves=False)
    assert d.ceded is True
    assert d.no_ha is True
    assert d.trigger_label == "barge-in(0.412)"


def test_losing_arbitration_cedes_without_a_record():
    """
    Something in the house is answering the utterance. A record would put a
    second "wake heard" in the history for a turn the neighbour is running,
    and a cue would report a race nobody asked about.
    """
    d = cede(serves=True, won_by="other-device")
    assert d.ceded is True
    assert d.no_ha is False
    assert d.trigger_label == ""


def test_the_winner_takes_the_turn():
    d = cede()
    assert d.ceded is False
    assert d.no_ha is False
    assert d.trigger_label == ""


def test_a_missing_pipeline_outranks_a_claim_the_caller_should_never_have_taken():
    """
    `can_serve_turn` is read before the claim, so a device with nothing behind
    it never takes a window away from one that could answer. The arbiter is
    not trusted for that here either: if a caller passes a win anyway, the
    missing pipeline is still the reason, and the reason that gets recorded.
    """
    d = cede(serves=False, won_by="test-device")
    assert d.no_ha is True
    assert d.trigger_label.startswith("barge-in(")


def test_the_recorded_label_reads_as_a_barge_and_not_a_wake_word():
    """
    Readers key on the trigger's "wakeword" PREFIX: `wake_word_phrase` sent to
    HA, and `_persist_turn`'s on-device shadow block, both test
    `startswith("wakeword")`, and the wake statistics in the Activity tab are
    read the same way. A borrowed prefix would file a barge inside the
    wake-word numbers and name a wake word this turn never sent to HA —
    "barge-in" is what a barge turn already records as.
    """
    label = cede(serves=False).trigger_label
    assert not label.startswith("wakeword")
    assert label.split("(")[0] == "barge-in"


# ── #195: holding a thinking-phase barge until somebody is still talking ─────

def window(opened=100.0, confirm=em_barge.CONFIRM_DEFAULT_MS, floor=0.0):
    return em_barge.FollowUpWindow(opened=opened, confirm_ms=confirm,
                                   floor=floor)


def test_nothing_is_decided_inside_the_wake_words_own_tail():
    """
    The frames just after a crossing are the wake word's OWN audio — the
    crossing arrives as the phrase completes — and they sit several frames
    above any speech bar. Counting them would make the window fire on the
    accidental barge it exists to catch, and a rule that always fires is not
    a rule. Same 240ms as em_esphome.VOICE_PREROLL_DISCARD.
    """
    w = window()
    assert w.feed(0.9, 100.0) == "", "the crossing frame itself"
    assert w.feed(0.9, 100.239) == "", "last frame inside the tail"


def test_speech_just_after_the_tail_takes_the_interrupting_turn():
    w = window()
    assert w.feed(0.9, 100.3) == "take"


def test_a_quiet_room_leaves_the_answer_alone():
    """
    The accidental barge, and the whole point of the window: HA's run was
    never touched, so its answer still arrives. Quiet frames say nothing while
    the window is open and the frame that closes it drops the barge.
    """
    w = window()
    assert w.feed(0.0, 100.3) == ""
    assert w.feed(0.0, 101.0) == ""
    assert w.feed(0.0, 101.5) == "drop"


def test_the_window_is_measured_from_the_crossing_not_from_the_first_frame_fed():
    """
    The same accounting as #139 on the turn clock. `opened` and `at` are
    CAPTURE times, so frames the link delivers late (or in a burst) are placed
    where the room made them rather than where they arrived — otherwise a
    stall hands the person a second window nobody granted them.
    """
    w = window()
    assert w.feed(0.0, 101.4) == "", "1.4s of quiet, still inside the window"
    assert w.feed(0.0, 101.5) == "drop", "measured from the crossing"


def test_the_window_closes_on_time_even_if_that_frame_is_loud():
    """
    "Take" needs its evidence INSIDE the window: the deadline is what bounds
    the latency a real interruption pays, so it is checked first and a frame
    past it drops however loud it is. Somebody who starts talking two seconds
    after their wake word was not talking over the answer.
    """
    assert window().feed(0.9, 101.6) == "drop"


def test_the_window_honours_the_configured_length():
    w = window(confirm=3000.0)
    assert w.feed(0.0, 102.9) == ""
    assert w.feed(0.0, 103.0) == "drop"


def test_the_bar_is_the_turn_paths_own_reading_of_speech():
    """
    em_esphome's `_is_speech` applies the same rule to the same 80ms frames,
    and two readings of "speech" that can disagree is how the window would
    take a barge the turn path calls silence. Three times the room's own
    floor, with an absolute bound for the floor-not-yet-warmed case.
    """
    assert em_barge.speech_bar(0.0) == 0.004
    assert em_barge.speech_bar(0.001) == 0.004, "the room has not warmed up"
    assert em_barge.speech_bar(0.01) == pytest.approx(0.03)
    assert em_barge.speech_bar(0.05) == pytest.approx(0.15)


def test_a_noisy_room_needs_a_frame_over_its_own_floor():
    # The floor a device measured, and a frame that is loud in absolute terms
    # but not against this room — a fan, not a person.
    w = window(floor=0.05)
    assert w.feed(0.10, 100.3) == ""
    assert w.feed(0.16, 100.4) == "take"


# ── The window is an option, and cannot be made useless (#195) ───────────────

def test_the_barge_window_default_does_not_move():
    # The point of #195 is that the window CAN move, not that it moved: every
    # existing deployment keeps the behaviour it has today.
    assert em_barge.CONFIRM_DEFAULT_MS == 1500.0
    assert em_barge.confirm_ms(None) == 1500.0
    assert em_barge.confirm_ms("") == 1500.0


def test_the_barge_window_takes_what_the_add_on_sends():
    assert em_barge.confirm_ms("2000") == 2000.0
    assert em_barge.confirm_ms(" 800 ") == 800.0
    assert em_barge.confirm_ms(900) == 900.0


def test_a_typed_barge_window_cannot_stop_the_controller_booting():
    # Read once at import, with whatever an operator put in the box.
    for junk in ("fifteen", "1500ms", "1e", "a second and a half", " "):
        assert em_barge.confirm_ms(junk) == em_barge.CONFIRM_DEFAULT_MS, junk
    # float() raises TypeError rather than ValueError for these, and either
    # one reaching the log is a controller that never boots.
    for not_a_number in (object(), ["1500"], {"ms": 1500}):
        assert em_barge.confirm_ms(not_a_number) == em_barge.CONFIRM_DEFAULT_MS


def test_a_barge_window_can_never_run_past_the_ceiling():
    """
    Past a couple of seconds this stops being a confirmation and becomes "the
    answer wins unless you say another wake word" — and every millisecond of
    it is latency added to a real interruption.
    """
    assert em_barge.confirm_ms("5000") == em_barge.CONFIRM_MAX_MS
    assert em_barge.confirm_ms("60000") == em_barge.CONFIRM_MAX_MS
    assert em_barge.CONFIRM_MAX_MS > em_barge.CONFIRM_DEFAULT_MS


def test_a_non_positive_barge_window_falls_back_rather_than_taking_or_dropping_everything():
    # 0 is not "take the barge at once" — no such state exists — and a window
    # of zero drops every barge. Both are decided here rather than by the
    # caller.
    for raw in ("0", "0.0", "-1500"):
        assert em_barge.confirm_ms(raw) == em_barge.CONFIRM_DEFAULT_MS, raw
