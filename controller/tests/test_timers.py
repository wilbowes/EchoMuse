"""Timer state and alarm audio (em_timers).

HA owns the countdown; the satellite only tracks live timers and decides when
to ring. These pin the ring-transition reducer (which is what the async
orchestrator in em_controller keys off), the spoken-dismissal matcher, and the
duck applied to the alert while someone speaks over it.
"""
import pytest

import em_timers as t


# ── TimerRegistry ────────────────────────────────────────────────────────────

def test_finished_starts_the_ring():
    reg = t.TimerRegistry()
    assert reg.apply(t.TIMER_STARTED, "a") == t.RING_NONE
    assert reg.ringing is False
    assert reg.apply(t.TIMER_FINISHED, "a") == t.RING_START
    assert reg.ringing is True


def test_cancel_of_finished_stops_the_ring():
    reg = t.TimerRegistry()
    reg.apply(t.TIMER_FINISHED, "a")
    # A spoken "stop" dismisses a finished timer as a CANCELLED event.
    assert reg.apply(t.TIMER_CANCELLED, "a") == t.RING_STOP
    assert reg.ringing is False


def test_cancel_of_running_timer_never_rings():
    reg = t.TimerRegistry()
    reg.apply(t.TIMER_STARTED, "a")
    # Cancelling a timer that was still counting down must not ring anything.
    assert reg.apply(t.TIMER_CANCELLED, "a") == t.RING_NONE
    assert reg.ringing is False


def test_updated_does_not_ring():
    reg = t.TimerRegistry()
    reg.apply(t.TIMER_STARTED, "a")
    assert reg.apply(t.TIMER_UPDATED, "a") == t.RING_NONE
    assert reg.ringing is False


def test_second_finished_while_ringing_is_no_transition():
    reg = t.TimerRegistry()
    reg.apply(t.TIMER_FINISHED, "a")
    # Already ringing — a second finished timer does not restart the ring.
    assert reg.apply(t.TIMER_FINISHED, "b") == t.RING_NONE
    assert reg.ringing is True
    # ...and the ring only stops once BOTH finished timers are dismissed.
    assert reg.apply(t.TIMER_CANCELLED, "a") == t.RING_NONE
    assert reg.ringing is True
    assert reg.apply(t.TIMER_CANCELLED, "b") == t.RING_STOP
    assert reg.ringing is False


def test_cancel_of_unknown_timer_is_harmless():
    # HA sends CANCELLED for a timer we cleared locally (button dismissal);
    # it must not error or spuriously stop.
    reg = t.TimerRegistry()
    assert reg.apply(t.TIMER_CANCELLED, "ghost") == t.RING_NONE
    assert reg.ringing is False


def test_unknown_event_type_is_ignored():
    reg = t.TimerRegistry()
    reg.apply(t.TIMER_FINISHED, "a")
    assert reg.apply(999, "a") == t.RING_NONE
    assert reg.ringing is True


def test_clear_reports_whether_it_was_ringing():
    reg = t.TimerRegistry()
    assert reg.clear() is False
    reg.apply(t.TIMER_FINISHED, "a")
    assert reg.clear() is True
    assert reg.ringing is False
    assert reg.active_count() == 0


# ── Stopping a ringing timer by voice ────────────────────────────────────────
# A wake word heard while a timer rings pauses the ring; anything spoken after
# it stops the ring, and silence lets it resume. DismissListen answers "did
# someone speak" from one speech probability per 80ms frame.

SPEECH, QUIET = 0.9, 0.05


def _feed(listen, probs):
    return [listen.push(p) for p in probs]


def test_two_consecutive_speech_frames_after_the_preroll_stop_the_ring():
    listen = t.DismissListen(preroll_frames=3)
    assert _feed(listen, [QUIET] * 3 + [QUIET, SPEECH, SPEECH]) == [False] * 5 + [True]
    assert listen.spoke is True


def test_the_wake_words_own_tail_does_not_count_as_an_answer():
    # The first frames after a wake carry the end of the wake word, which is
    # speech. Counting it would make every wake a dismissal, which is the
    # wake-alone rule this replaces: a false wake would silence an alarm.
    listen = t.DismissListen(preroll_frames=3)
    assert _feed(listen, [SPEECH] * 3 + [QUIET] * 40) == [False] * 43
    assert listen.spoke is False


def test_silence_never_stops_the_ring():
    listen = t.DismissListen(preroll_frames=3)
    assert not any(_feed(listen, [QUIET] * 50))
    assert listen.frames == 50


def test_one_loud_frame_is_not_speech():
    # A click or a clatter scores for a frame; a word lasts longer.
    listen = t.DismissListen(preroll_frames=0)
    assert not any(_feed(listen, [SPEECH, QUIET, SPEECH, QUIET, SPEECH, QUIET]))


def test_speech_frames_must_be_consecutive():
    listen = t.DismissListen(preroll_frames=0, speech_frames=3)
    assert _feed(listen, [SPEECH, SPEECH, QUIET, SPEECH, SPEECH, SPEECH]) == [False] * 5 + [True]


def test_the_threshold_is_inclusive():
    listen = t.DismissListen(preroll_frames=0)
    assert _feed(listen, [t.DISMISS_SPEECH_PROB, t.DISMISS_SPEECH_PROB]) == [False, True]
    below = t.DismissListen(preroll_frames=0)
    assert not any(_feed(below, [t.DISMISS_SPEECH_PROB - 0.01] * 10))


def test_the_verdict_latches():
    # Once someone has spoken, a pause in their sentence does not unsay it.
    listen = t.DismissListen(preroll_frames=0)
    assert _feed(listen, [SPEECH, SPEECH, QUIET, QUIET]) == [False, True, True, True]


def test_finished_is_speech_followed_by_a_pause():
    # The ring stops at `spoke`; the listening ring stays lit until
    # `finished`, so the person is not cut off mid-sentence.
    listen = t.DismissListen(preroll_frames=0, end_frames=5)
    _feed(listen, [SPEECH, SPEECH])
    assert listen.spoke and not listen.finished
    _feed(listen, [SPEECH] * 10 + [QUIET] * 4)
    assert not listen.finished, "four quiet frames is a breath, not the end"
    listen.push(QUIET)
    assert listen.finished


def test_a_pause_shorter_than_the_end_does_not_finish():
    # "Verona ... be quiet": a gap between words restarts the count.
    listen = t.DismissListen(preroll_frames=0, end_frames=5)
    _feed(listen, [SPEECH, SPEECH] + [QUIET] * 4 + [SPEECH] + [QUIET] * 4)
    assert not listen.finished
    listen.push(QUIET)
    assert listen.finished


def test_silence_alone_never_finishes():
    listen = t.DismissListen(preroll_frames=0)
    _feed(listen, [QUIET] * 100)
    assert not listen.spoke and not listen.finished


def test_peak_reports_the_loudest_frame_after_the_preroll():
    # Logged when nobody spoke, to tell a quiet room from a near miss.
    listen = t.DismissListen(preroll_frames=2)
    _feed(listen, [0.99, 0.99, 0.1, 0.4, 0.2])
    assert listen.peak == 0.4


def test_a_short_word_is_enough():
    # "Stop" is about 300ms: four 80ms frames, the middle ones voiced.
    listen = t.DismissListen(preroll_frames=3)
    assert _feed(listen, [QUIET] * 3 + [0.3, 0.8, 0.9, 0.4])[-2] is True


def test_the_listen_is_long_enough_to_say_something():
    # The hold outlives the wait in em_controller by a margin; the wait itself
    # has to cover a breath and a few words.
    assert 3.0 <= t.DISMISS_LISTEN_S <= 6.0


def test_no_word_list_remains():
    # The rule asks whether someone spoke, never what they said (Wil,
    # 2026-10-04): a transcript matcher needs a list per language.
    assert not hasattr(t, "is_dismissal")
    assert not hasattr(t, "is_dismissal_only")



def test_alert_sound_ships_with_its_licence():
    # CC BY 4.0 requires the attribution to travel with the audio, and there is
    # no synthesised fallback any more — a build without the file cannot ring,
    # so both the sound and its licence are load-bearing.
    import os
    assert os.path.exists(t.ALARM_SOUND_FILE), "the alert sound must ship"
    licence = os.path.join(os.path.dirname(t.ALARM_SOUND_FILE), "LICENSE.md")
    assert os.path.exists(licence), "bundled sound must ship with LICENSE.md"
    text = open(licence, encoding="utf-8").read()
    assert "CC BY" in text or "Creative Commons Attribution" in text
