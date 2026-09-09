"""The no-speech window must measure the user, not the link.

The regression these guard against is #139: a 1373ms delivery gap before the
first audio frame silently shortened the listening window and answered
`no_speech` to someone who spoke clearly. `test_late_audio_still_gets_a_full_
window` fails against the old turn-start-only logic, which is how it was
verified.
"""
import em_turnclock as tc


def test_speech_seen_never_closes_the_turn():
    # Once anything above the noise floor is heard, HA's VAD owns end-of-turn
    # and this decision must get out of the way — however long it has been.
    close, _ = tc.no_speech_verdict(now=1000.0, turn_start=0.0,
                                    listening_since=0.0, speech_seen=True)
    assert close is False


def test_silence_from_a_prompt_device_closes_on_time():
    # The ordinary accidental wake: audio flowing immediately, nobody speaks.
    # Behaviour here must be exactly what it always was.
    close, why = tc.no_speech_verdict(now=5.3, turn_start=0.0,
                                      listening_since=0.3, speech_seen=False)
    assert close is False, "5.0s of quiet is not yet over the limit"

    close, why = tc.no_speech_verdict(now=5.4, turn_start=0.0,
                                      listening_since=0.3, speech_seen=False)
    assert close is True
    assert "No speech" in why


def test_late_audio_still_gets_a_full_window():
    # THE #139 REGRESSION. Audio arrives 1.373s late, then the user speaks
    # 4s in. Measured from turn start that is 5.4s and the turn is already
    # dead; measured from first audio it is 4.0s and still listening.
    close, _ = tc.no_speech_verdict(now=5.4, turn_start=0.0,
                                    listening_since=1.373, speech_seen=False)
    assert close is False, "a slow link must not shorten the listening window"

    # ...and the window still ENDS, 5s after audio actually started.
    close, why = tc.no_speech_verdict(now=6.5, turn_start=0.0,
                                      listening_since=1.373, speech_seen=False)
    assert close is True
    assert "No speech" in why


def test_audio_that_never_arrives_still_ends_the_turn():
    # A device that dies right after the wake must not hold the HA pipeline
    # open. This is the bound on the fix above.
    close, _ = tc.no_speech_verdict(now=4.9, turn_start=0.0,
                                    listening_since=None, speech_seen=False)
    assert close is False

    close, why = tc.no_speech_verdict(now=5.1, turn_start=0.0,
                                      listening_since=None, speech_seen=False)
    assert close is True
    assert "No audio" in why, "must be distinguishable from a silent user"


def test_the_two_reasons_are_distinguishable():
    # They call for opposite investigations — a quiet room versus a dead link
    # — so the log line must not read the same for both.
    _, no_audio = tc.no_speech_verdict(now=99.0, turn_start=0.0,
                                       listening_since=None, speech_seen=False)
    _, no_speech = tc.no_speech_verdict(now=99.0, turn_start=0.0,
                                        listening_since=1.0, speech_seen=False)
    assert no_audio != no_speech


def test_limits_are_overridable_for_callers_and_tests():
    close, _ = tc.no_speech_verdict(now=2.1, turn_start=0.0,
                                    listening_since=0.0, speech_seen=False,
                                    no_speech_timeout=2.0)
    assert close is True


# ── HA's VAD failing to engage at all (#485) ────────────────────────────────
#
# The fault these guard against is not a late endpoint, it is no endpoint:
# HA's VoiceCommandSegmenter needs 0.3s of audio above its speech threshold
# before it will declare a command started, and a command that never clears
# that bar runs to HA's hardcoded 15s cap and is reported as an ordinary end
# of speech. Measured on the fleet at 3.2% of wake turns.

def test_ha_vad_engaged_keeps_end_of_turn():
    # The whole safety property. While HA's VAD is working it owns the
    # endpoint — its endpointing is model-driven and pause-tolerant, ours is
    # a threshold — so this must stay out of the way no matter how long the
    # user pauses mid-sentence.
    end, _ = tc.ha_vad_stalled_verdict(
        now=60.0, speech_seen=True, ha_vad_started=True,
        first_speech_at=1.0, last_speech_at=1.0,
    )
    assert end is False


def test_a_stalled_ha_vad_endpoints_the_turn():
    # Speech at 1.0s, quiet ever since, HA silent. At 4.5s we are past the
    # 2.5s grace and have 3.5s of quiet — end it rather than wait out HA's 15s.
    end, why = tc.ha_vad_stalled_verdict(
        now=4.5, speech_seen=True, ha_vad_started=False,
        first_speech_at=1.0, last_speech_at=1.0,
    )
    assert end is True
    assert "HA VAD never engaged" in why


def test_grace_outlasts_the_earliest_possible_vad_start():
    # microVAD answers nothing for its first 760ms and the segmenter then
    # needs 0.3s above threshold, so an STT_VAD_START cannot arrive before
    # ~1.06s; a real turn measured 1.077s. Firing inside that window would
    # cut off turns HA was about to endpoint perfectly well.
    end, _ = tc.ha_vad_stalled_verdict(
        now=1.0 + 1.5, speech_seen=True, ha_vad_started=False,
        first_speech_at=1.0, last_speech_at=1.0,
    )
    assert end is False, "must not pre-empt a VAD_START that is still coming"


def test_a_speaker_still_talking_is_never_cut_off():
    # Long command, well past the grace, but they are still going: the last
    # speech frame is 200ms old. Cutting here is worse than the stall.
    end, _ = tc.ha_vad_stalled_verdict(
        now=12.0, speech_seen=True, ha_vad_started=False,
        first_speech_at=1.0, last_speech_at=11.8,
    )
    assert end is False

    # They stop, and a full second later it ends.
    end, _ = tc.ha_vad_stalled_verdict(
        now=12.7, speech_seen=True, ha_vad_started=False,
        first_speech_at=1.0, last_speech_at=11.8,
    )
    assert end is False, "0.9s of quiet is not yet enough"
    end, _ = tc.ha_vad_stalled_verdict(
        now=12.8, speech_seen=True, ha_vad_started=False,
        first_speech_at=1.0, last_speech_at=11.8,
    )
    assert end is True


def test_a_pause_mid_sentence_restarts_the_silence_window():
    # Silence is measured from the LAST speech frame, so someone who pauses to
    # think and then carries on resets the clock rather than accumulating
    # toward an endpoint.
    end, _ = tc.ha_vad_stalled_verdict(
        now=8.0, speech_seen=True, ha_vad_started=False,
        first_speech_at=1.0, last_speech_at=7.5,
    )
    assert end is False


def test_no_speech_heard_leaves_the_no_speech_path_alone():
    # Nothing was ever heard, so this is an accidental wake and belongs to
    # no_speech_verdict. Two decisions acting on one turn would race.
    end, _ = tc.ha_vad_stalled_verdict(
        now=30.0, speech_seen=False, ha_vad_started=False,
        first_speech_at=None, last_speech_at=None,
    )
    assert end is False


def test_speech_seen_via_ha_without_our_own_marks_is_safe():
    # speech_seen can be set by HA's VAD start rather than our own RMS check,
    # leaving both marks None. That must not raise or fire.
    end, _ = tc.ha_vad_stalled_verdict(
        now=30.0, speech_seen=True, ha_vad_started=False,
        first_speech_at=None, last_speech_at=None,
    )
    assert end is False


def test_it_beats_has_15s_cap_by_a_wide_margin():
    # The user-visible point, and the bound is the GRACE, not the silence:
    # a short command ("what's the weather", the reported case) stops well
    # inside the window HA is still owed, so the turn ends at grace-plus-a-bit
    # rather than the moment they stop. That is deliberate — the alternative
    # is racing a VAD_START that was still coming.
    first_speech, speech_ended = 0.3, 1.5
    quiet_enough = speech_ended + tc.FALLBACK_SILENCE
    grace_over   = first_speech + tc.HA_VAD_GRACE
    assert quiet_enough < grace_over, "this is the grace-bounded case"

    end, _ = tc.ha_vad_stalled_verdict(
        now=quiet_enough, speech_seen=True, ha_vad_started=False,
        first_speech_at=first_speech, last_speech_at=speech_ended,
    )
    assert end is False, "the grace has to run out first"

    end, _ = tc.ha_vad_stalled_verdict(
        now=grace_over, speech_seen=True, ha_vad_started=False,
        first_speech_at=first_speech, last_speech_at=speech_ended,
    )
    assert end is True

    # maxwellh's turn took 15,056ms from the first audio frame to HA's
    # STT_VAD_END. Whichever bound binds, this is far inside it.
    assert grace_over < 4.0
