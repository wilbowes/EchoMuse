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

import re
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _turn_src() -> str:
    src = (CONTROLLER / "em_esphome.py").read_text()
    start = src.index("if self._tts_audio_url:")
    return src[start:start + 6000]


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

def test_each_cause_of_an_early_end_records_its_own_outcome():
    """
    Three different things stop a turn and they are three different things to
    find in the stats:

        dot button   -> "cancelled"   stops the response dead
        mute         -> "muted"       stops it dead AND deafens the device
        wake word    -> "barged"      stops it in favour of another turn

    Recording all three as "cancelled" is what made a user talking over the
    assistant indistinguishable from a button press. It also blunted #250's
    detection signature — a barged turn followed by a turn triggered by
    barge-in — which could only ever find a PLAYBACK barge while a thinking
    barge recorded "cancelled".
    """
    ctrl = (CONTROLLER / "em_controller.py").read_text()
    calls = re.findall(r"cancel_voice_turn\(([^)]*)\)", ctrl, re.S)
    assert len(calls) == 3, f"expected 3 call sites, got {len(calls)}"

    reasons = sorted(
        re.search(r'reason="(\w+)"', c).group(1)
        for c in calls if re.search(r'reason="(\w+)"', c)
    )
    assert reasons == ["barged", "cancelled", "muted"], (
        f"each cancel path must name its own cause; got {reasons}"
    )


def test_the_outcome_is_the_reason_at_every_phase():
    """
    A turn can end early during mic streaming, while waiting for TTS, or
    mid-response. The PHASE used to decide the wording, which is how #251
    fixed the barge it was not written for. All three now record the reason.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()
    reads = [l.strip() for l in src.splitlines()
             if l.strip() == 'why = self._turn_end_reason or "cancelled"']
    assert len(reads) == 3, (
        f"all three early-end sites must record the reason; found {len(reads)}"
    )
    assert 'trace.outcome = why' in src


def test_the_reason_survives_the_module_level_forwarder():
    """
    cancel_voice_turn forwards to the satellite. A parameter added to the
    signature and dropped in the body is a silent no-op: the call compiles,
    the reason never arrives, and every outcome quietly reads "cancelled".
    """
    src = (CONTROLLER / "em_esphome.py").read_text()
    fwd = src[src.index("def cancel_voice_turn("):]
    fwd = fwd[:fwd.index("\ndef ")]
    assert "reason=reason" in fwd, \
        "the forwarder must pass the reason through to cancel_turn"


def test_the_playback_barge_still_records_without_a_cancel():
    """
    abort_ha_run is the PLAYBACK barge path and deliberately does not mark
    the turn cancelled — that flag gates control flow. It must still record
    the reason, or the case #251 was written for goes back to reading "ok".
    """
    src = (CONTROLLER / "em_esphome.py").read_text()
    abort = src[src.index("def abort_ha_run"):]
    abort = abort[:abort.index("\n\n\n")] if "\n\n\n" in abort else abort[:2000]
    assert '"barged"' in abort and "_turn_end_reason" in abort
    assert "_turn_cancelled = True" not in abort, \
        "abort_ha_run must not mark the turn cancelled — see its docstring"


def test_the_reason_is_cleared_per_turn():
    """
    A sticky reason would label every later turn on the same satellite,
    which is the same wrong answer in the other direction.
    """
    src = (CONTROLLER / "em_esphome.py").read_text()
    start = src.index("self._turn_active           = True")
    assert "_turn_end_reason" in src[start:start + 400], \
        "_turn_end_reason must be cleared at turn start, not only in __init__"
