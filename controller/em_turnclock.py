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

import os

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

# Seconds to wait for Home Assistant's reply to BEGIN before the turn is
# abandoned. Hardcoded 30.0 until #805, where the reporter's tool-using
# conversation agent was making 5 model calls and 4 tool turns and was still
# going at 27s — a question needing one more round-trip crosses 30s in
# ordinary use, and the abandoned turn is silent: no TTS, no error anywhere
# the user can see, which from across the room is a deaf Echo.
#
# Reachable as the add-on option `tts_wait_timeout` (`EM_TTS_WAIT_TIMEOUT`).
# The default is deliberately unchanged, so no existing deployment moves.
TTS_WAIT_DEFAULT = 30.0

# The ceiling is not arbitrary. The thinking ring clears itself on
# `em_scenes.SPIN_TTL` (135s) measured from the same moment, so a wait that
# outlasts the ring does not fix the silent failure — it swaps one for
# another: an Echo that looks idle while the controller is still waiting.
TTS_WAIT_MAX = 120.0


def tts_wait_seconds(raw, default=TTS_WAIT_DEFAULT, maximum=TTS_WAIT_MAX):
    """The reply wait in seconds, from the raw environment string.

    Read at import with whatever an operator typed into the add-on box, so
    this must never raise and must never return something that leaves the
    turn unbounded: anything that is not a positive number falls back to the
    default, and anything above the ring's dead-man is clamped to it.
    """
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum)


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


# Hard stop after speech has started. HA's own cap is 15s and the streaming
# phase has a 20s backstop; under a TV or a fan, HA's VAD starts and never
# ends, and both of those are too long to stand in front of. 7s is a command,
# not a conversation.
MAX_LISTEN_S = 7.0

# How long audio may stay quiet or below listen confidence before the
# controller ends the turn. Matches the device's vadSilenceMs default.
TRAIL_CUTOFF_S = 0.9

# Silero probability that still counts as the person talking. Real speech
# measured 0.72–1.00 and non-speech 0.02–0.03, so a TV that keeps the RMS
# gate and HA's VAD open sits under this.
LISTEN_CONFIDENCE = 0.35


class ListenClock:
    """Speech and confidence marks for one turn. All times are monotonic."""

    def __init__(self):
        self.first_speech_at = None
        self.last_speech_at = None
        self.speech_started_at = None
        self.last_confident_at = None

    def mark_started(self, now):
        if self.speech_started_at is None:
            self.speech_started_at = now

    def hear(self, now, speech, prob, device):
        """One released frame. Held frames are not passed here.

        `prob` is Silero's score, or None when the gate model is absent and
        RMS speech is the only confidence signal.
        """
        if speech:
            self.last_speech_at = now
            if self.first_speech_at is None:
                self.first_speech_at = now
            self.mark_started(now)
        limit = getattr(device, "listen_confidence", LISTEN_CONFIDENCE)
        confident = speech if prob is None else prob >= limit
        if confident:
            self.last_confident_at = now
            self.mark_started(now)


def listen_cap_verdict(now, speech_started_at, last_confident_at,
                       max_listen_s=MAX_LISTEN_S,
                       trail_cutoff_s=TRAIL_CUTOFF_S):
    """End the turn once speech has run too long, or gone quiet / unsure.

    No speech start means no end: the no-speech timeout owns that case.
    The cap is measured from speech start and fires even while HA's VAD is
    running. The trail is measured from the last confident frame, or from
    speech start when nothing confident has arrived.

    Returns (end, reason); reason is "" when end is False.
    """
    if speech_started_at is None:
        return False, ""
    if now - speech_started_at >= max_listen_s:
        return True, f"Listen cap {max_listen_s:.1f}s after speech start"
    ref = speech_started_at if last_confident_at is None else last_confident_at
    quiet = now - ref
    if quiet >= trail_cutoff_s:
        return True, (
            f"Trailing silence / low confidence {quiet:.1f}s "
            f"(cutoff {trail_cutoff_s:.1f}s)"
        )
    return False, ""


def endpoint_verdict(now, speech_seen, ha_vad_started, clock, device):
    """Listen cap and trail first, then HA's VAD having failed to start."""
    end, why = listen_cap_verdict(
        now, clock.speech_started_at, clock.last_confident_at,
        getattr(device, "max_listen_s", MAX_LISTEN_S),
        getattr(device, "trail_cutoff_s", TRAIL_CUTOFF_S),
    )
    if end:
        return True, why
    return ha_vad_stalled_verdict(
        now, speech_seen, ha_vad_started,
        clock.first_speech_at, clock.last_speech_at,
    )


def _clamp_int(raw, default, lo, hi):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, value))


def _clamp_float(raw, default, lo, hi):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, value))


def listen_limits(config):
    """(max_listen_s, trail_cutoff_s, listen_confidence) from a config dict."""
    cfg = config or {}
    return (
        _clamp_int(cfg.get("maxListenMs", 7000), 7000, 2000, 20000) / 1000.0,
        _clamp_int(cfg.get("trailCutoffMs", 900), 900, 200, 3000) / 1000.0,
        _clamp_float(cfg.get("listenConfidence", LISTEN_CONFIDENCE),
                     LISTEN_CONFIDENCE, 0.0, 1.0),
    )


def apply_listen(device, config):
    """Copy the listen limits onto the device. Both registration and a live push."""
    device.max_listen_s, device.trail_cutoff_s, device.listen_confidence = (
        listen_limits(config)
    )


def _env_get(env, name):
    if env is None:
        env = os.environ
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    return raw


def overlay_fleet(cfg, env=None):
    """Add-on listen options on top of the stored fleet config.

    Unset variables leave the stored value. A set variable always wins, so
    saving the fleet page cannot freeze the add-on option at first boot.
    A Dot that overrides Advanced still wins later, in em_config_sections.merge.
    """
    out = dict(cfg)
    ms = _env_get(env, "EM_MAX_LISTEN_MS")
    if ms is not None:
        out["maxListenMs"] = _clamp_int(ms, 7000, 2000, 20000)
    trail = _env_get(env, "EM_TRAIL_CUTOFF_MS")
    if trail is not None:
        out["trailCutoffMs"] = _clamp_int(trail, 900, 200, 3000)
    conf = _env_get(env, "EM_LISTEN_CONFIDENCE")
    if conf is not None:
        out["listenConfidence"] = _clamp_float(conf, LISTEN_CONFIDENCE, 0.0, 1.0)
    return out
