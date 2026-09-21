"""
A turn plays its reply from HA's early streaming signal, not from TTS_END.

HA hands the satellite the TTS URL in RUN_START and sends INTENT_PROGRESS
`tts_start_streaming == "1"` when the reply's first text arrives; TTS_END, the
only other place the URL appears, comes after the whole reply. Waiting for it
put first audio for a two-paragraph reply at 5.83 s after STT, against 3.02 s
when playing from the signal (2026-09-21). See em_earlytts.

Two halves, as for em_barge: the decisions and the stream helper are pure and
tested directly; the wiring in em_esphome cannot be imported here (aiohttp,
a database, a device), so it is pinned against the source, as an AST check for
the reason recorded in test_stream_teardown.py.
"""

import ast
import asyncio
import sys
import pathlib

CONTROLLER = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROLLER))

import pytest

import em_earlytts as early


# ── the decision ────────────────────────────────────────────────────────────

URL = "http://ha.local:8123/api/tts_proxy/abc.flac"


def start(progress=None, announced=URL, playing=None, cancelled=False):
    return early.should_start(
        progress={"tts_start_streaming": "1"} if progress is None else progress,
        announced_url=announced, playing_url=playing, cancelled=cancelled,
    )


def test_the_signal_with_an_announced_url_starts_playback():
    assert start() is True


@pytest.mark.parametrize("progress", [
    {},                                # a progress event with no field
    {"tts_start_streaming": "0"},
    {"tts_start_streaming": ""},
    {"tts_start_streaming": "true"},   # ESPHome compares against the literal "1"
    {"other": "1"},
])
def test_only_the_literal_one_starts_playback(progress):
    assert start(progress=progress) is False


@pytest.mark.parametrize("announced", [None, ""])
def test_no_announced_url_means_nothing_to_play(announced):
    assert start(announced=announced) is False


def test_a_second_signal_does_not_restart_a_url_already_playing():
    assert start(playing=URL) is False


def test_a_cancelled_turn_does_not_start_speaking():
    assert start(cancelled=True) is False


def test_run_start_url():
    assert early.run_start_url({"url": URL}) == URL
    assert early.run_start_url({}) is None
    assert early.run_start_url({"url": ""}) is None
    assert early.run_start_url({"other": "x"}) is None


# ── abortable_stream ────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def test_passes_every_chunk_through_and_closes_the_source():
    closed = []

    async def source():
        try:
            for i in range(3):
                yield bytes([i])
        finally:
            closed.append(True)

    async def go():
        return [c async for c in early.abortable_stream(source(), asyncio.Event())]

    assert run(go()) == [b"\x00", b"\x01", b"\x02"]
    assert closed == [True]


def test_abort_ends_a_stream_blocked_on_the_next_chunk():
    """HA never closes an early response when the reply fails."""
    closed = []

    async def source():
        try:
            yield b"a"
            await asyncio.sleep(3600)
            yield b"b"
        finally:
            closed.append(True)

    async def go():
        abort, out = asyncio.Event(), []

        async def consume():
            async for c in early.abortable_stream(source(), abort):
                out.append(c)

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.05)
        assert out == [b"a"] and not task.done()
        abort.set()
        await asyncio.wait_for(task, 2)
        return out

    assert run(go()) == [b"a"]
    assert closed == [True]


def test_abort_already_set_ends_a_stream_that_never_yields():
    async def source():
        await asyncio.sleep(3600)
        yield b"never"

    async def go():
        abort = asyncio.Event()
        abort.set()
        return [c async for c in early.abortable_stream(source(), abort)]

    assert run(asyncio.wait_for(go(), 2)) == []


def test_cancelling_the_consumer_while_a_chunk_is_pending_closes_cleanly():
    """
    A barge-in during playback cancels the consumer while the helper waits for
    the next chunk. The first version raised `aclose(): asynchronous generator
    is already running`, left the source open and leaked the fetch task.
    """
    closed, errors = [], []

    async def source():
        try:
            yield b"a"
            await asyncio.sleep(3600)
            yield b"b"
        finally:
            closed.append(True)

    async def go():
        async def consume():
            try:
                async for _ in early.abortable_stream(source(), asyncio.Event()):
                    pass
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # noqa: BLE001 - the point is to see it
                errors.append(repr(e))
                raise

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await asyncio.sleep(0.05)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    leaked = run(go())
    assert errors == []
    assert closed == [True]
    assert leaked == []


def test_stopping_early_closes_the_source():
    closed = []

    async def source():
        try:
            for i in range(100):
                yield bytes([i])
                await asyncio.sleep(0)
        finally:
            closed.append(True)

    async def go():
        stream = early.abortable_stream(source(), asyncio.Event())
        async for c in stream:
            if c == b"\x03":
                break
        await stream.aclose()

    run(go())
    assert closed == [True]


def test_source_errors_propagate():
    async def source():
        yield b"a"
        raise RuntimeError("fetch failed")

    async def go():
        return [c async for c in early.abortable_stream(source(), asyncio.Event())]

    with pytest.raises(RuntimeError, match="fetch failed"):
        run(go())


# ── wait_until ──────────────────────────────────────────────────────────────

def test_wait_until_returns_at_once_when_already_true():
    assert run(early.wait_until(lambda: True, 1.0)) is True


def test_wait_until_sees_a_flag_set_meanwhile():
    async def go():
        flag = []
        asyncio.get_running_loop().call_later(0.1, flag.append, 1)
        return await early.wait_until(lambda: bool(flag), 2.0, step=0.01)

    assert run(go()) is True


def test_wait_until_gives_up_after_the_timeout():
    assert run(early.wait_until(lambda: False, 0.1, step=0.01)) is False


# ── the wiring in em_esphome ────────────────────────────────────────────────

TREE = ast.parse((CONTROLLER / "em_esphome.py").read_text())


def _function(name):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in em_esphome.py — renamed?")


def _calls(node, dotted):
    """Every Call under `node` whose func unparses to `dotted`."""
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and ast.unparse(n.func) == dotted]


def _branch(func, event):
    """The `if`/`elif` under `func` testing for ET.VOICE_ASSISTANT_<event>."""
    for n in ast.walk(func):
        if isinstance(n, ast.If) and ast.unparse(n.test).endswith(f"ET.VOICE_ASSISTANT_{event}"):
            return n
    raise AssertionError(f"no branch for VOICE_ASSISTANT_{event} in {func.name}")


def _own_body_nodes(branch):
    """Nodes in this branch's body, not in the elif chained after it."""
    return [n for stmt in branch.body for n in ast.walk(stmt)]


def test_module_is_imported():
    assert any(
        isinstance(n, ast.Import) and any(a.name == "em_earlytts" for a in n.names)
        for n in ast.walk(TREE)
    ), "em_esphome no longer imports em_earlytts"


def test_run_start_keeps_the_announced_url():
    body = ast.Module(body=_branch(_function("_handle_voice_event"), "RUN_START").body, type_ignores=[])
    assert _calls(body, "em_earlytts.run_start_url"), "RUN_START no longer records the announced URL"


def test_intent_progress_starts_playback_through_should_start():
    branch = _branch(_function("_handle_voice_event"), "INTENT_PROGRESS")
    body = ast.Module(body=branch.body, type_ignores=[])
    assert _calls(body, "em_earlytts.should_start"), "INTENT_PROGRESS no longer asks should_start"
    text = ast.unparse(body)
    assert "self._tts_streamed_early = True" in text
    assert "self._tts_event.set()" in text


def test_tts_end_does_not_replace_a_url_already_playing():
    branch = _branch(_function("_handle_voice_event"), "TTS_END")
    guarded = [
        n for n in _own_body_nodes(branch)
        if isinstance(n, ast.If)
        and "_tts_streamed_early" in ast.unparse(n.test)
        and "self._tts_audio_url = url" in ast.unparse(ast.Module(body=n.body, type_ignores=[]))
    ]
    assert guarded, "TTS_END assigns _tts_audio_url unconditionally again"


def test_an_ha_error_ends_an_early_stream():
    branch = _branch(_function("_handle_voice_event"), "ERROR")
    body = ast.Module(body=branch.body, type_ignores=[])
    assert _calls(body, "self._tts_abort.set"), (
        "an HA ERROR no longer aborts an early TTS fetch; HA never closes it, "
        "so the fetch would sit out its 60s read timeout"
    )


def _turn_function():
    """The function that waits on _tts_event and plays the reply."""
    for node in ast.walk(TREE):
        if isinstance(node, ast.AsyncFunctionDef) and _calls(node, "post_turn_play"):
            return node
    raise AssertionError("the voice-turn function (calls post_turn_play) not found")


def test_an_early_stream_is_played_through_abortable_stream():
    turn = _turn_function()
    wrapped = [
        n for n in ast.walk(turn)
        if isinstance(n, ast.If)
        and "_tts_streamed_early" in ast.unparse(n.test)
        and _calls(ast.Module(body=n.body, type_ignores=[]), "em_earlytts.abortable_stream")
    ]
    assert wrapped, "an early stream is no longer wrapped in abortable_stream"


def test_continue_conversation_gets_a_beat_after_an_early_stream():
    turn = _turn_function()
    assert _calls(turn, "em_earlytts.wait_until"), (
        "after an early stream the turn no longer waits for INTENT_END, which "
        "carries continue_conversation"
    )


def test_every_turn_starts_with_the_early_state_cleared():
    """State from an earlier turn must not release the next turn's stream."""
    reset = next(
        n for n in ast.walk(TREE)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and _calls(n, "self._tts_event.clear")
    )
    assigned = {
        ast.unparse(t)
        for n in ast.walk(reset) if isinstance(n, ast.Assign)
        for t in n.targets
    }
    assert {"self._early_tts_url", "self._tts_streamed_early"} <= assigned
    assert _calls(reset, "self._tts_abort.clear")
