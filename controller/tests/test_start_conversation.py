"""
Announce-then-listen: `assist_satellite.start_conversation` and
`assist_satellite.ask_question` (#335, #396).

Home Assistant reuses `VoiceAssistantAnnounceRequest` for both, with
`start_conversation` (field 4) marking the ones that must be followed by a
listening turn. Two things decide whether the feature exists at all, and
neither of them shows up in a log:

  * HA filters the eligible targets on `VoiceAssistantFeature.START_CONVERSATION`
    in `DeviceInfoResponse`, so without that bit the device never appears in the
    action's target picker — no error, just an empty list;
  * the flag on the request itself, which we used to read `media_id` and `text`
    from and drop.

The third thing is invisible even once it works. `ask_question` sets
`end_stage = STT` on HA's own pipeline (`assist_satellite/entity.py`, keyed on
its answer future), so the run ends after STT_END with no INTENT_END and no
TTS — while our RUN_END guard waits for INTENT_END. Questions get answered
correctly and the device then sits out the 30s TTS wait and records a timeout.

Pure-logic and source-shape only: the suite does not import em_esphome
(zeroconf, aiohttp, the database), which is why the sequencing that CAN be
tested lives in em_announce.
"""

import asyncio
import re
from pathlib import Path

import em_announce

CONTROLLER = Path(__file__).resolve().parents[1]
ESPHOME_SRC = (CONTROLLER / "em_esphome.py").read_text()
CONTROLLER_SRC = (CONTROLLER / "em_controller.py").read_text()


def _strip_py_comments(src: str) -> str:
    """
    Comments AND docstrings. A guard that strips only `#` lines matches the
    prose explaining the rule it is enforcing and passes whatever the code
    does — this tree has done that three times now.
    """
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    return "\n".join(re.sub(r"#.*$", "", line) for line in src.splitlines())


ESPHOME_CODE = _strip_py_comments(ESPHOME_SRC)
CONTROLLER_CODE = _strip_py_comments(CONTROLLER_SRC)


def fetch_returning(pcm):
    async def _fetch(url):
        return pcm

    return _fetch


class Replies:
    def __init__(self):
        self.calls = []

    def __call__(self, ok):
        self.calls.append(ok)


# ── The preannounce chime ────────────────────────────────────────────────────


def test_the_chime_plays_before_the_message():
    played = []

    async def play(pcm):
        played.append(pcm)

    async def fetch(url):
        return url.encode()

    ok = asyncio.run(
        em_announce.run(
            "message",
            fetch=fetch,
            play=play,
            on_finished=Replies(),
            preannounce_media_id="chime",
        )
    )
    assert ok is True
    assert played == [b"chime", b"message"], (
        "the attention chime must arrive before the message it announces"
    )


def test_no_chime_is_the_ordinary_case():
    played = []

    async def play(pcm):
        played.append(pcm)

    asyncio.run(
        em_announce.run(
            "message",
            fetch=fetch_returning(b"pcm"),
            play=play,
            on_finished=Replies(),
        )
    )
    assert played == [b"pcm"]


def test_a_failing_chime_still_plays_the_message():
    """
    The chime is a cue for the message, not the message. Losing it is worth a
    log line; swallowing an announcement over it is not — and on
    `start_conversation` the announcement is a question somebody is waiting to
    answer.
    """
    played = []

    async def fetch(url):
        if url == "chime":
            raise RuntimeError("404 from the TTS proxy")
        return b"pcm"

    async def play(pcm):
        played.append(pcm)

    replies = Replies()
    ok = asyncio.run(
        em_announce.run(
            "message",
            fetch=fetch,
            play=play,
            on_finished=replies,
            preannounce_media_id="chime",
        )
    )
    assert played == [b"pcm"]
    assert ok is True, "the chime failing must not report the message as failed"
    assert replies.calls == [True]


def test_a_wedged_chime_cannot_outlast_the_announcement_cap():
    """
    One budget covers both, or a chime that never returns parks HA for its own
    five minutes holding `_is_announcing`.
    """
    replies = Replies()

    async def play(pcm):
        await asyncio.Event().wait()

    ok = asyncio.run(
        em_announce.run(
            "message",
            fetch=fetch_returning(b"pcm"),
            play=play,
            on_finished=replies,
            timeout=0.05,
            preannounce_media_id="chime",
        )
    )
    assert ok is False
    assert replies.calls == [False], "HA must still be answered"


# ── The capability HA filters on ─────────────────────────────────────────────


def test_start_conversation_is_gated_on_the_microphone():
    """
    A satellite that cannot listen must not offer to. It would appear in the
    action's target picker and answer every question with silence.
    """
    assert "VoiceAssistantFeature.START_CONVERSATION" in ESPHOME_CODE, (
        "the START_CONVERSATION feature bit is not advertised anywhere — HA "
        "filters announce-then-listen targets on it, so the device will not "
        "appear as an eligible target"
    )
    flags = ESPHOME_CODE[ESPHOME_CODE.index("VOICE_ASSISTANT_FLAGS = int("):]
    flags = flags[: flags.index(")")]
    assert "START_CONVERSATION" not in flags, (
        "START_CONVERSATION is in the unconditional flag constant — it must be "
        "advertised per device, gated on the mic capability"
    )
    gate = ESPHOME_CODE[ESPHOME_CODE.index("def _voice_assistant_flags"):]
    gate = gate[: gate.index("\n    def ", 10)]
    assert '_device_has("mic")' in gate


def test_device_info_advertises_the_per_device_flags():
    """
    The gate is worth nothing if DeviceInfoResponse still sends the constant.
    """
    info = ESPHOME_CODE[ESPHOME_CODE.index("DeviceInfoResponse("):]
    info = info[: info.index(")\n")]
    assert "voice_assistant_feature_flags=self._voice_assistant_flags()" in info


# ── The request field, and what happens after the audio ──────────────────────


def test_the_announce_handler_reads_both_dropped_fields():
    """
    `start_conversation` and `preannounce_media_id` are fields 4 and 3 of the
    same message, and both were carried by the vendored protobuf and read by
    nothing.
    """
    handler = ESPHOME_CODE[ESPHOME_CODE.index("def handle_message"):]
    handler = handler[: handler.index("\n    def ", 10)]
    assert "msg.start_conversation" in handler
    assert "msg.preannounce_media_id" in handler


def test_listening_starts_after_ha_has_been_told_the_announcement_finished():
    """
    HA blocks on AnnounceFinished for the whole announcement, and
    `async_internal_ask_question` only arms its answer future once
    `async_start_conversation` returns. Listening first means listening while
    HA still believes the satellite is speaking.
    """
    body = ESPHOME_CODE[ESPHOME_CODE.index("async def _run_announce"):]
    body = body[: body.index("\n    async def _start_conversation_turn")]
    assert body.index("em_announce.run(") < body.index("_start_conversation_turn()"), (
        "the listening turn is started before the announcement is reported "
        "finished"
    )


def test_an_absent_device_does_not_pretend_to_listen():
    """
    No physical Dot means nothing to listen with, and the announcement has
    already reported that it did not play.
    """
    body = ESPHOME_CODE[ESPHOME_CODE.index("async def _start_conversation_turn"):]
    body = body[: body.index("\n    async def ", 10)]
    assert "if start is None:" in body
    assert "return" in body


# ── The pipeline HA truncates at STT ─────────────────────────────────────────


def test_an_answer_only_run_ends_on_stt_not_intent():
    """
    `ask_question` ends HA's pipeline at STT, so INTENT_END structurally never
    arrives and the RUN_END guard would hold the turn for the full 30s TTS
    wait — the question answered correctly, the device unresponsive after it,
    and every one of them recorded as a timeout.
    """
    branch = ESPHOME_CODE[ESPHOME_CODE.index("if self._intent_ended or self._turn_cancelled"):]
    branch = branch[:400]
    assert "self._answer_only and self._stt_ended" in branch, (
        "RUN_END is still gated on INTENT_END alone — an answered question "
        "will park the turn until the TTS wait expires"
    )


def test_the_answer_only_flag_comes_from_the_turns_own_label():
    """
    Derived from the trace's trigger rather than plumbed through as a
    parameter, so the flag and the activity stats cannot disagree about what
    started the turn.
    """
    assert "trace.trigger == CONVERSATION_TRIGGER" in ESPHOME_CODE


def test_stt_end_sets_the_marker_it_is_read_by():
    stt = ESPHOME_CODE[ESPHOME_CODE.index("VOICE_ASSISTANT_STT_END"):]
    stt = stt[: stt.index("VOICE_ASSISTANT_INTENT_END")]
    assert "self._stt_ended = True" in stt


# ── The turn itself ──────────────────────────────────────────────────────────


def test_the_trigger_is_neither_a_wake_word_nor_a_button():
    """
    `wake_word_phrase` is keyed on the label starting with "wakeword", and the
    dashboard's wake statistics are grouped by it. An HA-initiated turn
    borrowing either label puts turns nobody spoke a wake word for into the
    wake-word numbers.
    """
    label = re.search(r'CONVERSATION_TRIGGER = "([^"]+)"', ESPHOME_CODE)
    assert label, "CONVERSATION_TRIGGER is not defined"
    assert not label.group(1).startswith("wakeword")
    assert label.group(1) != "button"


def test_the_conversation_turn_discards_no_preroll():
    """
    There is no wake-word tail to trim, so discarding frames here clips the
    first words of the answer — the bug C3 fixed on the button path.
    """
    body = CONTROLLER_CODE[CONTROLLER_CODE.index("async def _start_conversation("):]
    body = body[: body.index("\n        await esphome.device_connected(")]
    assert "is_wakeword=False" in body
    assert "esphome.CONVERSATION_TRIGGER" in body


def test_a_muted_device_still_runs_the_turn_rather_than_going_silent():
    """
    The mute is enforced on the device — it rejects every `mic_start` while
    muted and the ADC is muted in hardware — so nothing is captured either way.
    What the controller must NOT do is refuse by staying silent:
    `async_internal_ask_question` awaits its answer future with no timeout, so
    a satellite that never runs a pipeline hangs the caller's script for good.
    Running the turn bounds it — the streaming phase gives up at its cap and HA
    ends the run with no answer.
    """
    body = CONTROLLER_CODE[CONTROLLER_CODE.index("async def _start_conversation("):]
    body = body[: body.index("\n        await esphome.device_connected(")]
    muted = body[body.index("if _d.muted:"):]
    muted = muted[: muted.index("_d.cancel_event.clear()")]
    assert "return" not in muted, (
        "a muted device refuses the conversation by returning — HA's "
        "ask_question will then wait on its answer future forever"
    )
    assert "_run_voice_locked" in body
