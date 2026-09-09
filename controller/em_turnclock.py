"""When a voice turn should stop waiting, as a pure function.

Split out of `em_esphome._stream_mic_audio` for the reason `em_button.decide`
and `em_linkauth.decide` were: the test suite does not import em_esphome, so
this was timing logic with two clocks and two limits and no coverage — and it
cost a real voice turn (#139) before anyone looked at it.

THE TWO CLOCKS ANSWER DIFFERENT QUESTIONS, and conflating them is the bug this
module exists to prevent:

  - "Has the user said anything?" can only be asked once their audio is
    arriving. It is measured from the FIRST REAL FRAME.
  - "Is any audio coming at all?" is measured from turn start, and is about
    the device and the link, not the person.

Measured from turn start alone, a slow link masquerades as a silent user. On
this fleet that is not hypothetical: ordinary WiFi loss (5-7%, on a link whose
actual latency is 6ms) drives TCP retransmission delays of 400-1400ms, and a
measured 1373ms gap before the first frame shortened a 5s window to 3.6s and
answered `no_speech` to someone who had spoken clearly. The device had
captured that audio perfectly and TCP was holding it.
"""

# Seconds of quiet, measured from the first real audio frame, before an
# accidental wake is closed quietly. Matches the device's own noSpeechTimeout.
NO_SPEECH_TIMEOUT = 5.0

# Seconds from the first frame of speech before we accept that HA's VAD is not
# going to engage on this turn. Home Assistant's microVAD returns a "no answer"
# sentinel for its first 760ms whatever the input (measured across digital
# silence, room tone at 0.0021 and 0.0043, continuous speech and a 1kHz tone),
# and its VoiceCommandSegmenter then needs 0.3s of audio above its speech
# threshold before it declares a command started. So the earliest an
# STT_VAD_START can physically arrive is ~1.06s of audio, and a real turn was
# measured at 1.077s. This is more than twice that: it must never expire on a
# turn HA was about to endpoint normally.
HA_VAD_GRACE = 2.5

# Seconds of consecutive non-speech before we endpoint a turn ourselves, once
# HA's VAD has been ruled out. Deliberately longer than both of the endpointing
# rules it stands in for — HA's own default silence window is 0.7s and the
# device's vadSilenceMs default is 900ms — because our test is a crude RMS bar
# against the room's noise floor rather than a model, so it needs more evidence
# before it acts. Cutting somebody off mid-sentence is worse than the stall
# this exists to prevent.
FALLBACK_SILENCE = 1.0

# Seconds to wait for the first real frame before giving up on the turn
# entirely. Bounds the other side: audio that never arrives must still end the
# turn, or a device that dies immediately after the wake holds the HA pipeline
# open until HA's own timeout. A healthy device delivers its first
# post-preroll frame in ~300ms, so this only elapses on a genuinely stalled
# link and the ordinary path never reaches it.
FIRST_AUDIO_GRACE = 5.0


def no_speech_verdict(now, turn_start, listening_since, speech_seen,
                      no_speech_timeout=NO_SPEECH_TIMEOUT,
                      first_audio_grace=FIRST_AUDIO_GRACE):
    """Should this turn close quietly as a no-speech?

    All times are monotonic seconds from the same clock.

    `listening_since` is when the first real (post-preroll) frame arrived, or
    None if none has. `speech_seen` is whether anything above the room's noise
    floor has been heard — once true this never closes the turn, because
    end-of-turn belongs to HA's VAD from that point, or failing that to
    `ha_vad_stalled_verdict` below.

    Returns (close, reason); reason is "" when close is False.
    """
    if speech_seen:
        return False, ""

    if listening_since is not None:
        waited, limit = now - listening_since, no_speech_timeout
        why = f"No speech within {limit}s of the first audio frame"
    else:
        waited, limit = now - turn_start, first_audio_grace
        why = f"No audio at all within {limit}s of turn start"

    if waited > limit:
        return True, why
    return False, ""


def ha_vad_stalled_verdict(now, speech_seen, ha_vad_started,
                           first_speech_at, last_speech_at,
                           grace=HA_VAD_GRACE,
                           fallback_silence=FALLBACK_SILENCE):
    """Should the controller endpoint this turn itself, HA's VAD having failed?

    All times are monotonic seconds from the same clock.

    Home Assistant's endpointing can fail to engage AT ALL, and when it does
    the turn does not end late — it runs to HA's own hardcoded 15s cap and
    reports that as an ordinary end of speech. The satellite cannot see the
    difference: `VoiceCommandSegmenter` sets `timed_out` and nothing in the
    whole of home-assistant/core ever reads it, so a timeout and a real
    endpoint arrive as the same `STT_VAD_END`.

    The failure is structural rather than flaky. The segmenter needs 0.3s of
    audio scored above its speech threshold before it will declare a command
    started, and a command that does not clear that bar can never fire
    STT_VAD_START however long it waits. Measured on this fleet: 3.2% of wake
    turns (27 of 845) ended in a 250ms bin at 15.25s, against 0-3 turns in
    every neighbouring bin, each with exactly 15,120ms of audio. Half of them
    returned nothing at all.

    So the discriminator is the ABSENCE of STT_VAD_START, not a timer — the
    same shape as the RUN_END-with-no-RUN_START rule in em_esphome, and for
    the same reason: the protocol's structure says what a timeout cannot.
    While HA's VAD is engaged this never fires and HA keeps end-of-turn, which
    is where it belongs; its endpointing is model-driven and pause-tolerant
    and ours is a threshold.

    `first_speech_at` / `last_speech_at` are when speech was first and most
    recently heard by the controller's own SNR-relative check. Silence is
    measured from the LAST speech frame rather than tracked as a run, so a
    turn whose frames stop arriving mid-utterance still ends: an empty queue
    and a quiet room are the same thing to a user waiting for an answer.

    Returns (end, reason); reason is "" when end is False.
    """
    if not speech_seen or ha_vad_started:
        return False, ""

    if first_speech_at is None or last_speech_at is None:
        return False, ""

    if now - first_speech_at < grace:
        return False, ""

    quiet = now - last_speech_at
    if quiet < fallback_silence:
        return False, ""

    return True, (
        f"HA VAD never engaged and {quiet:.1f}s quiet since speech "
        f"(controller-side endpoint)"
    )
