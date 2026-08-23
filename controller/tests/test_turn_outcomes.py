"""
#251: a turn cancelled during playback must not record outcome=ok.

A barge-in that cut a response off mid-sentence was persisted as
outcome=ok with tts_bytes=0 — indistinguishable in the activity stats
from a turn that answered. An outcome that flatters the system is the
worst kind to get wrong: the rollup is what you consult when deciding
whether something is broken, and this one hid #250 (self-interruption)
for two days of false-barge investigation.

em_esphome imports openwakeword/aiohttp and is deliberately not
importable here (see conftest), so these are shape guards on the shipped
source.
"""

from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _turn_src() -> str:
    src = (CONTROLLER / "em_esphome.py").read_text()
    start = src.index("if self._tts_audio_url:")
    return src[start:start + 6000]


def test_a_cancel_during_playback_records_barged_not_ok():
    src = _turn_src()
    assert 'trace.outcome = "barged"' in src, \
        "a turn cut off mid-response must be distinguishable from an answer"
    assert "_turn_cancelled" in src.split('trace.outcome = "barged"')[0], \
        "the barged outcome must be gated on the cancel flag"
    # The finally-block default must not be able to overwrite it back to ok:
    # the barged branch has to sit BEFORE the unconditional default runs.
    default = src.index("if not trace.outcome:")
    assert src.index('"barged"') < default


def test_barged_is_distinct_from_the_pre_playback_cancel():
    """
    "cancelled" already covers cuts during mic streaming and the TTS wait -
    nothing had been said yet. "barged" means a response had begun. Both
    must exist; collapsing them would blur 'user pressed the button early'
    into 'something interrupted the answer'.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()
    assert src.count('trace.outcome = "cancelled"') >= 2, \
        "the pre-playback cancel outcomes must stay"
    assert 'trace.outcome = "barged"' in src


def test_self_interruption_is_detectable_from_the_persisted_fields():
    """
    The motivating case (#250): the device triggering on its own narration.
    Neither row alone says why - but a barged turn immediately followed by a
    turn whose trigger is "barge-in" is the signature, which requires both
    fields persisted as-is.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()
    rec = src[src.index("turn_record = {"):]
    rec = rec[:rec.index("}")]
    assert '"outcome"' in rec and '"trigger"' in rec, \
        "outcome and trigger must both reach the turns table"


# ─── Both barge paths, because covering one is what caused the bug ───────────

def test_the_playback_barge_path_also_marks_the_outcome():
    """
    #251's fix keyed on `_turn_cancelled` and therefore covered the barge it
    was NOT written for.

    There are two paths and they set different flags:

        barge during THINKING  -> cancel_voice_turn()  -> _turn_cancelled
        barge during PLAYBACK  -> abort_ha_run()       -> (nothing, by design)

    abort_ha_run deliberately does not mark the turn cancelled — that flag
    gates on_thinking, on_stt_end and the intent-ended check, so marking a
    delivered response cancelled would change what the turn DOES. Its
    docstring says so.

    The consequence, measured on hardware 2026-08-23: "Sing a song." was cut
    off mid-song by a real barge, logged `Cancelled during streamed buffer
    drain`, and persisted `outcome=ok`. The exact reading #251 existed to
    prevent, on the exact case its description describes.

    So abort_ha_run must record something, and the outcome must read both.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()

    abort = src[src.index("def abort_ha_run"):]
    abort = abort[:abort.index("\n\n\n")] if "\n\n\n" in abort else abort[:2000]
    assert "_turn_barged" in abort, (
        "abort_ha_run is the PLAYBACK barge path and must record that the "
        "turn was cut off, even though it must not mark it cancelled"
    )
    assert "_turn_cancelled = True" not in abort, (
        "abort_ha_run must NOT set _turn_cancelled — that flag changes "
        "control flow, and its docstring explains why this path avoids it"
    )

    branch = src[src.index("# #251: cut off mid-response") - 200:]
    branch = branch[:branch.index('trace.outcome = "barged"')]
    assert "_turn_barged" in branch and "_turn_cancelled" in branch, (
        "the outcome must read BOTH flags — one per barge path"
    )


def test_the_barged_flag_is_reset_per_turn():
    """
    A sticky flag would mark every later turn on the same satellite as
    barged, which is the same class of wrong answer in the other direction.
    Reset alongside _turn_cancelled at turn start, not only at construction.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()
    start = src.index("self._turn_active           = True")
    window = src[start:start + 400]
    assert "_turn_barged" in window, (
        "_turn_barged must be cleared where _turn_cancelled is, at turn "
        "start — clearing it only in __init__ leaves it set for the life "
        "of the connection"
    )
