"""
Whose shell connection is this: a transfer's, or a dashboard terminal's.

A device opens `/shell` when it is asked to, and the controller has two kinds
of asker. A programmatic session (OTA, wake word assets, payload syncs) wants
the socket itself. A dashboard terminal wants it proxied to a browser. Both
leave a future in `_shell_pending`, and the terminal also leaves an entry in
`_shell_dashboard`.

`handle_shell` used to decide by whether a dashboard entry EXISTED, which is
a different question from whether the pending request was the terminal's. A
transfer starting while a terminal entry was on record had its connection
proxied to that terminal, and when the proxy ended its future was resolved
with None: every transfer to that Echo failed with
"'NoneType' object has no attribute 'send'". The entry could also outlive its
browser tab, because a terminal whose Echo never dialled waited for ever.

Split out of em_controller for em_linkauth's reason: the suite cannot import
it.
"""

import asyncio
from typing import Optional

NONE = "none"
PROGRAMMATIC = "programmatic"
INTERACTIVE = "interactive"

# How long a terminal waits for the Echo to open its side. The programmatic
# path allows the same.
TERMINAL_DIAL_TIMEOUT_S = 15.0

# Why a transfer was refused, shown in the device log.
TERMINAL_OPEN = "a terminal session is open on this Echo — close it and try again"


class Terminal:
    """A dashboard terminal's request: its browser socket and its own future."""

    def __init__(self, ws, done: asyncio.Future):
        self.ws = ws
        self.done = done
        # Set by handle_shell when the Echo's connection starts being proxied.
        self.dialled = asyncio.Event()


def route(pending: Optional[asyncio.Future], terminal: Optional[Terminal]) -> str:
    """What the Echo's new shell connection is for.

    Interactive only when the pending request IS the terminal's own. A
    terminal on record beside somebody else's request does not claim it.
    """
    if pending is None or pending.done():
        return NONE
    if terminal is not None and terminal.done is pending:
        return INTERACTIVE
    return PROGRAMMATIC


async def wait_for_dial(terminal: Terminal, timeout: float = TERMINAL_DIAL_TIMEOUT_S) -> bool:
    """Wait for the Echo to open the terminal's shell. False if it never did.

    Returns True as well when the session has already ended, so the caller
    falls through to its own cleanup either way.
    """
    dialled = asyncio.ensure_future(terminal.dialled.wait())
    try:
        await asyncio.wait({dialled, terminal.done}, timeout=timeout,
                           return_when=asyncio.FIRST_COMPLETED)
        return terminal.dialled.is_set() or terminal.done.done()
    finally:
        dialled.cancel()
