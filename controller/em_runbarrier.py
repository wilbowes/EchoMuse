"""
em_runbarrier.py — serialising ESPHome pipeline runs across a barge-in.

The ESPHome voice protocol carries NO run identifier. `VoiceAssistantEventResponse`
is an event type plus a list of name/value pairs and nothing else, so a client
structurally cannot tell which run an event belongs to. The protocol is built
for strictly one pipeline run at a time per connection, and the SATELLITE is
what enforces that.

Home Assistant does not enforce it for us. `handle_pipeline_start` clears the
audio queue and cancels the TTS streaming task, then overwrites `_pipeline_task`
*without cancelling the previous one* — so starting a second run orphans the
first, which keeps emitting events onto the same socket.

Barge-in is the obvious place two runs overlap, and not the only one: any
turn that stops waiting while HA is still working leaves a live run behind
it — a `timeout` after 30s, or a `no_speech` that never sends the end
sentinel. Those are caught at turn teardown rather than per-path, because
fixing the path that noticed is how `no_speech` sat uncovered while
`timeout` was fixed beside it (#333). Measured on 2026-08-17: five
barge-ins, five interrupting turns dead in 4-17ms with zero audio captured. The
aborted run's RUN_END arrived ~4ms after the new turn started, the new turn had
not yet seen a RUN_START of its own, and the "HA ended a run it never started"
branch read it as terminal.

HA acknowledges an abort with **no wire message at all** (`_abort_pipeline` just
cancels the task), so the barrier cannot be a timeout. It is ordering: after an
abort, discard every event until the next RUN_START, which is necessarily ours —
HA emits RUN_START only when it creates a pipeline, we created exactly one more,
and the connection is processed in order.

Split out of em_esphome for the reason em_linkauth and em_button.decide are: the
test suite does not import em_esphome (zeroconf, aiohttp, the database), so
logic living there has no coverage, and this is a state machine whose two
failure modes are both silent — swallow too little and the interrupting turn
dies; swallow too much and the satellite goes deaf.
"""


class RunBarrier:
    """
    Two flags and four transitions.

    `armed` is set when a run is aborted, during the turn being abandoned.
    `active` is the barrier in force, during the turn that follows. They are
    separate because the arm has to survive the next turn's state reset to
    reach the turn it protects.
    """

    __slots__ = ("armed", "active")

    def __init__(self) -> None:
        self.armed = False
        self.active = False

    def abort(self) -> None:
        """A run was aborted upstream; the next turn inherits the barrier."""
        self.armed = True

    def begin_turn(self) -> None:
        """
        Start of a turn. Takes ownership of a pending arm.

        The arm is consumed here rather than merely read, so exactly one turn
        is ever protected. A barrier that outlived its turn would discard the
        events of turns that have nothing to do with the abort.
        """
        self.active = self.armed
        self.armed = False

    def end_turn(self) -> None:
        """
        End of a turn. Drops the barrier whether or not a RUN_START arrived.

        This is the bound. If HA never sends the RUN_START we are waiting for
        — the connection dropped, the pipeline failed to start — the barrier
        must not persist into the next turn, or the satellite discards every
        event forever and no turn can ever complete again.
        """
        self.active = False

    def discards(self, is_run_start: bool) -> bool:
        """
        Should this event be dropped?

        RUN_START releases the barrier and is itself DELIVERED, not swallowed:
        the caller needs it to set `_run_started`, and eating it would re-arm
        the very bug this exists to fix for the turn's own terminal RUN_END.
        """
        if not self.active:
            return False
        if is_run_start:
            self.active = False
            return False
        return True
