"""
A dashboard terminal and a file transfer never take each other's shell.

Reported from the field, 2026-10-13: every wake word asset push to one Echo
failed with "'NoneType' object has no attribute 'send'" a second after it
started. `handle_shell` proxied any connection to a terminal whenever a
terminal entry existed for that Echo, including the connection a transfer had
asked for, and resolved the transfer's future with None when the proxy ended.

What is pinned: the connection goes to whoever asked for it
(`em_shellplane.route`), a transfer refuses while a terminal is open and says
why, a terminal whose Echo never dials gives up, and neither call site can go
back to deciding by the entry's existence.
"""

import ast
import asyncio
from pathlib import Path

import pytest

import em_api
import em_shellplane as sp

from test_shell_owner import FakeLive, FakeWS, _reset

HERE = Path(__file__).resolve().parent.parent


async def _requested():
    """Until the transfer has left its request. Python 3.10's wait_for takes
    several loop turns to acquire the lock; 3.12's takes none."""
    while "dev" not in em_api._shell_pending:
        await asyncio.sleep(0)


def test_a_transfers_connection_is_not_proxied_to_a_terminal_on_record():
    """The reported fault. The old rule was `dashboard entry is not None`."""

    async def main():
        loop = asyncio.get_running_loop()
        terminal = sp.Terminal(FakeWS(), loop.create_future())
        transfer = loop.create_future()

        assert sp.route(transfer, terminal) == sp.PROGRAMMATIC

        # Controls: the terminal's own request is still proxied, and a
        # transfer with no terminal about is still a transfer.
        assert sp.route(terminal.done, terminal) == sp.INTERACTIVE
        assert sp.route(transfer, None) == sp.PROGRAMMATIC

    asyncio.run(main())


def test_no_request_or_a_finished_one_claims_nothing():
    async def main():
        loop = asyncio.get_running_loop()
        terminal = sp.Terminal(FakeWS(), loop.create_future())
        assert sp.route(None, None) == sp.NONE
        assert sp.route(None, terminal) == sp.NONE
        terminal.done.set_result(None)
        assert sp.route(terminal.done, terminal) == sp.NONE

    asyncio.run(main())


def test_a_transfer_refuses_while_a_terminal_is_open_and_holds_nothing():
    _reset()

    async def main():
        loop = asyncio.get_running_loop()
        terminal = sp.Terminal(FakeWS(), loop.create_future())
        em_api._shell_pending["dev"] = terminal.done
        em_api._shell_dashboard["dev"] = terminal
        live = FakeLive("dev")

        with pytest.raises(RuntimeError, match="terminal session is open"):
            await em_api._get_device_shell_ws(live)

        assert live.sent == [], "shell_open would cut the terminal off"
        assert em_api._shell_pending["dev"] is terminal.done, "the terminal's request was replaced"
        assert not em_api._shell_lock["dev"].locked(), "the refusal kept the lock"
        assert "dev" not in em_api._shell_owner

        # Control: with the terminal gone the same call opens a shell.
        em_api._shell_pending.clear()
        em_api._shell_dashboard.clear()

        async def answer():
            await _requested()
            em_api._shell_pending["dev"].set_result(FakeWS())

        t = asyncio.create_task(answer())
        assert await em_api._get_device_shell_ws(live) is em_api._shell_ws["dev"]
        await t

    asyncio.run(main())
    _reset()


def test_a_shell_that_resolves_to_nothing_raises_instead_of_returning_none():
    """The caller's next line is `ws.send`."""
    _reset()

    async def main():
        live = FakeLive("dev")

        async def answer():
            await _requested()
            em_api._shell_pending["dev"].set_result(None)

        t = asyncio.create_task(answer())
        with pytest.raises(RuntimeError, match="ended before it opened"):
            await em_api._get_device_shell_ws(live)
        await t

        assert not em_api._shell_lock["dev"].locked()
        assert "dev" not in em_api._shell_ws
        assert "dev" not in em_api._shell_pending

    asyncio.run(main())
    _reset()


def test_a_terminal_gives_up_on_an_echo_that_never_dials():
    async def main():
        loop = asyncio.get_running_loop()

        never = sp.Terminal(FakeWS(), loop.create_future())
        assert await sp.wait_for_dial(never, timeout=0.05) is False

        dialled = sp.Terminal(FakeWS(), loop.create_future())
        loop.call_later(0.01, dialled.dialled.set)
        assert await sp.wait_for_dial(dialled, timeout=5) is True

        # A session that ended while waiting falls through to cleanup.
        ended = sp.Terminal(FakeWS(), loop.create_future())
        loop.call_later(0.01, ended.done.set_result, None)
        assert await sp.wait_for_dial(ended, timeout=5) is True

    asyncio.run(main())


def _function(path, name):
    tree = ast.parse((HERE / path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _calls(fn):
    return {
        f"{n.func.value.id}.{n.func.attr}"
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
    }


def test_handle_shell_asks_route_and_marks_the_dial():
    fn = _function("em_controller.py", "handle_shell")
    assert "em_shellplane.route" in _calls(fn)
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "dialled" in attrs, "the terminal is never told its Echo dialled"


def test_the_terminal_bounds_its_wait_and_removes_only_its_own_entries():
    fn = _function("em_api.py", "_ws_shell")
    assert "em_shellplane.wait_for_dial" in _calls(fn)
    assert "em_shellplane.Terminal" in _calls(fn)
    # An unguarded pop removes a later session's request.
    pops = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "pop" and isinstance(n.func.value, ast.Name)
        and n.func.value.id in ("_shell_pending", "_shell_dashboard")
    ]
    guarded = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
        and isinstance(n.test.ops[0], ast.Is)
    ]
    assert len(pops) == 2 and len(guarded) >= 2
