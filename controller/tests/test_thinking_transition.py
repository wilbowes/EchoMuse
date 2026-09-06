"""
Every route that ends a turn's speech enters `thinking` the same way.

A turn can conclude in two places — HA's STT_VAD_END event, and our own device
VAD sentinel in _stream_mic_audio — and both mean the same thing: the user
stopped talking. Only the first was wired to on_thinking, so a turn ending on
the sentinel sent `end=True` to HA and then held the listening ring through
the entire STT-to-TTS window. Measured at ~11s on the live fleet (#370).

It presented as "a button-triggered turn is slower to notice I've finished
than a wake word one", and the trigger is a red herring: there is exactly ONE
deliberate branch on it in the whole turn path (preroll_discard, which a
button turn legitimately skips). Which endpoint route wins is a race between
two VAD timers. The trigger only correlated with the winner — 33/33 wake turns
ended on HA's VAD, 11/11 button turns on the sentinel, 2026-08-28 — and
nothing holds that correlation on a slower link.

So these tests pin the convergence, not the button. The trigger decides how a
turn starts; nothing after it differs.
"""

import ast
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
SRC = (CONTROLLER / "em_esphome.py").read_text()
TREE = ast.parse(SRC)


def _method(name: str):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in em_esphome.py — renamed?")


def _self_calls(node) -> list[str]:
    """`self.foo(...)` attribute names called anywhere under node."""
    out = []
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "self"):
            out.append(sub.func.attr)
    return out


def test_on_thinking_is_only_ever_invoked_from_one_place():
    """
    The invariant that makes a third endpoint route impossible to get wrong:
    nothing calls `self._on_thinking()` directly. Add a new way for a turn to
    end, call _enter_thinking, and the ring is correct for free.
    """
    offenders = []
    for node in ast.walk(TREE):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name == "_enter_thinking":
            continue
        if "_on_thinking" in _self_calls(node):
            offenders.append(node.name)

    assert not offenders, (
        f"{offenders} call self._on_thinking() directly. Route it through "
        "_enter_thinking() instead — it is idempotent and is what every "
        "endpoint path shares."
    )


def test_enter_thinking_is_idempotent():
    """
    Both routes genuinely fire on a slow turn: the sentinel sends end=True and
    HA can still emit STT_VAD_END for the same silence. Entering twice would
    restart the spinner mid-turn.
    """
    fn = _method("_enter_thinking")
    src = ast.unparse(fn)
    assert "_thinking_entered" in src, (
        "_enter_thinking must guard on _thinking_entered — both endpoint "
        "routes can fire within one turn."
    )
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.If) and "_thinking_entered" in ast.unparse(n.test)]
    assert guards, "_thinking_entered must be TESTED, not merely assigned."


def test_the_flag_is_reset_per_turn():
    """
    Cleared where the other per-turn flags are. A flag that survived a turn
    would suppress the transition for every turn after the first.
    """
    fn = _method("run_esphome_voice_turn")
    assigned = any(
        isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_thinking_entered"
                for t in n.targets)
        for n in ast.walk(fn)
    )
    assert assigned, (
        "_thinking_entered must be reset in run_esphome_voice_turn beside "
        "_ha_vad_end.clear() — otherwise only the first turn of a connection "
        "ever shows thinking."
    )

    # It has to sit with the other per-turn flags, not somewhere that only
    # runs on a subset of turns.
    block = ast.unparse(fn)
    assert "_ha_vad_end.clear()" in block, (
        "the per-turn reset block moved out of run_esphome_voice_turn; this "
        "test is anchored to it and needs to follow."
    )


def test_both_known_endpoint_routes_enter_thinking():
    """
    The two routes that exist today. If a third is added, the test above is
    what catches it; this one pins that neither of these two regresses.
    """
    handler = _method("_handle_voice_event")
    assert "_enter_thinking" in _self_calls(handler), (
        "the STT_VAD_END branch must call _enter_thinking"
    )

    stream = _method("_stream_mic_audio")
    assert "_enter_thinking" in _self_calls(stream), (
        "the device VAD sentinel must call _enter_thinking — this is the "
        "route that was missing it (#370)"
    )


def test_a_no_speech_timeout_does_not_enter_thinking():
    """
    The one end=True that is NOT a user finishing: the no-speech timeout
    closes HA's pipeline having heard nothing. Showing the spinner there
    would claim the Echo is working on a request nobody made.
    """
    stream = _method("_stream_mic_audio")

    for node in ast.walk(stream):
        if not isinstance(node, ast.If):
            continue
        if "VAD_SENTINEL_TIMEOUT" not in ast.unparse(node.test):
            continue
        assert "_enter_thinking" not in _self_calls(node), (
            "the no-speech timeout branch must not enter thinking — nothing "
            "was said, so there is nothing to think about."
        )
        return
    raise AssertionError(
        "the VAD_SENTINEL_TIMEOUT branch was not found in _stream_mic_audio; "
        "if it moved, this test needs to follow it."
    )
