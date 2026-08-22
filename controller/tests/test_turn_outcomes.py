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
