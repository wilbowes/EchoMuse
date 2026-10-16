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


# ── #805: the reply wait is configurable, and cannot be made unbounded ───────

def test_the_reply_wait_default_does_not_move():
    # The whole point of #805 is that the wait CAN move, not that it moved:
    # every existing deployment keeps the behaviour it has today.
    assert tc.TTS_WAIT_DEFAULT == 30.0
    assert tc.tts_wait_seconds(None) == 30.0
    assert tc.tts_wait_seconds("") == 30.0


def test_the_reply_wait_takes_what_the_add_on_sends():
    assert tc.tts_wait_seconds("60") == 60.0
    assert tc.tts_wait_seconds(" 75 ") == 75.0
    assert tc.tts_wait_seconds(90) == 90.0


def test_a_typed_value_cannot_stop_the_controller_booting():
    # Read once at import, with whatever an operator put in the box.
    for junk in ("ninety", "60s", "1e", "thirty seconds", " "):
        assert tc.tts_wait_seconds(junk) == tc.TTS_WAIT_DEFAULT, junk


def test_a_reply_wait_can_never_run_longer_than_the_thinking_ring():
    # em_scenes.SPIN_TTL is 135s and the ring is the only thing telling the
    # user the turn is alive. A wait that outlasts it replaces the silent
    # abort with a silent idle ring, which is the same bug wearing a hat.
    assert tc.tts_wait_seconds("135") == tc.TTS_WAIT_MAX
    assert tc.tts_wait_seconds("600") == tc.TTS_WAIT_MAX
    assert tc.TTS_WAIT_MAX < 135.0, "must stay inside em_scenes.SPIN_TTL"


def test_a_non_positive_wait_falls_back_rather_than_expiring_at_once():
    # "0" would abandon every turn the instant it started.
    assert tc.tts_wait_seconds("0") == tc.TTS_WAIT_DEFAULT
    assert tc.tts_wait_seconds("-30") == tc.TTS_WAIT_DEFAULT


def test_listen_cap_is_seven_seconds_after_speech_starts():
    start = 0.0
    end, _ = tc.listen_cap_verdict(now=start + 6.9, speech_started_at=start,
                                   last_confident_at=start + 6.9)
    assert end is False
    end, why = tc.listen_cap_verdict(now=start + 7.0, speech_started_at=start,
                                     last_confident_at=start + 7.0)
    assert end is True
    assert "Listen cap" in why
    assert tc.MAX_LISTEN_S == 7.0


def test_trailing_low_confidence_ends_before_the_cap():
    start = 0.0
    end, _ = tc.listen_cap_verdict(now=0.8, speech_started_at=start,
                                   last_confident_at=0.0)
    assert end is False
    end, why = tc.listen_cap_verdict(now=0.9, speech_started_at=start,
                                     last_confident_at=0.0)
    assert end is True
    assert "low confidence" in why


def test_confident_speech_keeps_the_turn_open_until_the_cap():
    end, _ = tc.listen_cap_verdict(now=6.0, speech_started_at=0.0,
                                   last_confident_at=5.5)
    assert end is False


def test_the_limits_are_overridable():
    end, _ = tc.listen_cap_verdict(now=10.0, speech_started_at=0.0,
                                   last_confident_at=9.5,
                                   max_listen_s=20.0, trail_cutoff_s=2.0)
    assert end is False
    end, _ = tc.listen_cap_verdict(now=20.0, speech_started_at=0.0,
                                   last_confident_at=19.0,
                                   max_listen_s=20.0, trail_cutoff_s=2.0)
    assert end is True


def test_the_cap_fires_while_ha_vad_is_running():
    class Dev:
        max_listen_s = 7.0
        trail_cutoff_s = 0.9
    clock = tc.ListenClock()
    clock.speech_started_at = 0.0
    clock.last_confident_at = 6.9
    clock.first_speech_at = 0.0
    clock.last_speech_at = 6.9
    end, why = tc.endpoint_verdict(7.0, True, True, clock, Dev())
    assert end is True
    assert "Listen cap" in why


def test_a_quiet_trail_ends_a_turn_ha_already_started():
    class Dev:
        max_listen_s = 7.0
        trail_cutoff_s = 0.9
    clock = tc.ListenClock()
    clock.mark_started(1.0)
    end, why = tc.endpoint_verdict(2.0, True, True, clock, Dev())
    assert end is True
    assert "low confidence" in why


def test_silero_confidence_ignores_loud_noise():
    class Dev:
        listen_confidence = 0.35
    clock = tc.ListenClock()
    clock.hear(1.0, speech=True, prob=0.9, device=Dev())
    clock.hear(1.5, speech=True, prob=0.1, device=Dev())
    assert clock.speech_started_at == 1.0
    assert clock.last_confident_at == 1.0


def test_without_silero_rms_speech_counts_as_confident():
    clock = tc.ListenClock()
    clock.hear(2.0, speech=True, prob=None, device=object())
    assert clock.last_confident_at == 2.0
    clock.hear(3.0, speech=False, prob=None, device=object())
    assert clock.last_confident_at == 2.0


def test_addon_env_overlays_fleet_and_a_dot_override_wins(monkeypatch, tmp_path):
    import em_config_sections
    import em_db
    stored = {"maxListenMs": 8000, "trailCutoffMs": 1500,
              "listenConfidence": 0.35, "vadThreshold": 0.001}
    monkeypatch.delenv("EM_MAX_LISTEN_MS", raising=False)
    monkeypatch.delenv("EM_TRAIL_CUTOFF_MS", raising=False)
    monkeypatch.delenv("EM_LISTEN_CONFIDENCE", raising=False)
    assert tc.overlay_fleet(stored)["maxListenMs"] == 8000
    fleet = tc.overlay_fleet(stored, {
        "EM_MAX_LISTEN_MS": "5000",
        "EM_TRAIL_CUTOFF_MS": "nope",
        "EM_LISTEN_CONFIDENCE": "1.5",
    })
    assert fleet["maxListenMs"] == 5000
    assert fleet["trailCutoffMs"] == 900
    assert fleet["listenConfidence"] == 1.0
    dot = {"maxListenMs": 12000, "vadThreshold": 0.002}
    assert em_config_sections.merge(fleet, dot, ["advanced"])["maxListenMs"] == 12000
    assert em_config_sections.merge(fleet, dot, [])["maxListenMs"] == 5000
    em_db.init(str(tmp_path / "em.db"))
    try:
        monkeypatch.setenv("EM_MAX_LISTEN_MS", "6000")
        assert em_db.get_global_device_config()["maxListenMs"] == 6000
    finally:
        em_db._conn.close()
        em_db._conn = None


def test_apply_listen_is_on_register_and_on_a_live_push():
    import _sources
    assert "em_turnclock.apply_listen(device, config)" in _sources.def_source("handle_control")
    assert "em_turnclock.apply_listen(live, effective)" in _sources.def_source("apply_live_config")


def test_endpoint_falls_through_when_the_cap_has_not_fired():
    class Dev:
        max_listen_s = 7.0
        trail_cutoff_s = 0.9
    clock = tc.ListenClock()
    clock.speech_started_at = 0.0
    clock.last_confident_at = 2.9
    clock.first_speech_at = 0.0
    clock.last_speech_at = 1.5
    end, why = tc.endpoint_verdict(3.0, True, False, clock, Dev())
    assert end is True
    assert "HA VAD" in why


def test_a_bad_confidence_keeps_the_default():
    out = tc.overlay_fleet({"listenConfidence": 0.2},
                           {"EM_LISTEN_CONFIDENCE": "high"})
    assert out["listenConfidence"] == tc.LISTEN_CONFIDENCE


def test_apply_listen_copies_seconds():
    class Dev:
        pass
    dev = Dev()
    tc.apply_listen(dev, {"maxListenMs": 8000, "trailCutoffMs": 400,
                          "listenConfidence": 0.2})
    assert dev.max_listen_s == 8.0
    assert dev.trail_cutoff_s == 0.4
    assert dev.listen_confidence == 0.2
