"""
`_stream_tts_audio_once` must kill ffmpeg BEFORE cancelling its feeder task.

Found while reviewing PR #400, which proposed closing stdin first to fix #252
(an unhandled InvalidStateError logged on every barge-in cancel). Measured over
20 runs of a standalone repro of this teardown shape, with nobody draining the
subprocess's stdout — the barge-in case, where the consumer has stopped
iterating the generator:

    teardown ordering            InvalidStateError   teardown hung
    cancel, then kill (before)         20/20             20/20
    close stdin, cancel, kill          20/20             20/20
    kill first                          0/20              0/20

The hang is the more serious half and it is not what #252 reported. ffmpeg's
stdout pipe fills because nobody is reading it, so ffmpeg blocks on write and
stops reading stdin; the feeder is stuck in drain(); cancelling it runs its
finally, which awaits `stdin.wait_closed()` — a flush that needs ffmpeg to read
and so never completes. `gather()` never returns and `proc.kill()` is never
reached. Killing first breaks both pipes, so those awaits raise instead.

This is an AST check rather than a text search, for the reason recorded in
test_deploy.py's source-guard notes: a grep for the call finds the comment
explaining it and passes anyway, which has now happened several times in this
suite. It is also the only shape available — the suite does not import
em_esphome (aiohttp, a database and a device), so the ordering cannot be
exercised directly.
"""

import ast
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
FUNC = "_stream_tts_audio_once"


def _finally_body() -> list[ast.stmt]:
    """The `finally:` block of _stream_tts_audio_once."""
    tree = ast.parse((CONTROLLER / "em_esphome.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == FUNC:
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Try) and stmt.finalbody:
                    return stmt.finalbody
            raise AssertionError(f"{FUNC} has no try/finally — restructured?")
    raise AssertionError(f"{FUNC} not found in em_esphome.py — renamed?")


def _first_lineno(body: list[ast.stmt], predicate) -> int | None:
    """Line of the earliest call anywhere under `body` matching `predicate`."""
    hits = [
        node.lineno
        for stmt in body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Call) and predicate(node)
    ]
    return min(hits) if hits else None


def _is_attr_call(node: ast.Call, attr: str) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr == attr


def test_ffmpeg_is_killed_before_the_feeder_is_cancelled():
    body = _finally_body()

    kill = _first_lineno(body, lambda n: _is_attr_call(n, "kill"))
    cancel = _first_lineno(body, lambda n: _is_attr_call(n, "cancel"))

    assert kill is not None, (
        f"{FUNC}'s finally no longer kills the subprocess — a barge-in would "
        "leave ffmpeg running"
    )
    assert cancel is not None, (
        f"{FUNC}'s finally no longer cancels the feeder — restructured?"
    )
    assert kill < cancel, (
        f"{FUNC} cancels the feeder (line {cancel}) before killing ffmpeg "
        f"(line {kill}). With nobody draining stdout the feeder's own finally "
        "awaits stdin.wait_closed(), which cannot complete while ffmpeg is "
        "blocked writing — teardown hangs and #252 comes back. Kill first."
    )


def test_the_kill_precedes_the_gather_that_waits_on_the_feeder():
    """gather() is what actually blocks, so the kill must come before it too."""
    body = _finally_body()

    kill = _first_lineno(body, lambda n: _is_attr_call(n, "kill"))
    gather = _first_lineno(
        body,
        lambda n: _is_attr_call(n, "gather")
        or (isinstance(n.func, ast.Name) and n.func.id == "gather"),
    )

    assert gather is not None, f"{FUNC}'s finally no longer gathers its tasks"
    assert kill < gather, (
        f"{FUNC} awaits gather() (line {gather}) before killing ffmpeg "
        f"(line {kill}) — that is the await which never returns"
    )


def test_teardown_does_not_wait_on_stdin_flushing():
    """
    The rejected shape from PR #400: closing stdin and awaiting wait_closed()
    in this finally. It does not fix #252 (measured 20/20 either way) and adds
    a second unbounded await ahead of the kill, on a flush that needs a
    blocked ffmpeg to read.
    """
    body = _finally_body()

    waited = _first_lineno(body, lambda n: _is_attr_call(n, "wait_closed"))

    assert waited is None, (
        f"{FUNC}'s finally awaits stdin.wait_closed() at line {waited}. That "
        "flush cannot complete while ffmpeg is blocked writing to a stdout "
        "nobody is draining, so it hangs teardown. Kill the process instead — "
        "that breaks the pipe and the feeder's own close() then returns."
    )
