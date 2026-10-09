"""
#315: a control-plane drop used to tear down a device's services
immediately — HA entities deregistered, BLE proxy dropped, media session
killed — and rebuilt all of it when the device returned seconds later.
The data plane has had DATA_RECONNECT_GRACE_S for exactly this reason;
the control plane never got the equivalent.

em_controller is deliberately not importable here (see conftest); these
are shape guards on the shipped source.
"""

import ast
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _finally_src() -> str:
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index("log.info(f\"[control] Device disconnected")
    end = src.index("# ─── Data plane handler", start)
    return src[start:end]


def test_teardown_is_deferred_not_immediate():
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index('log.info(f"[control] Device disconnected')
    seg = src[start:start + 1200]
    task_call = seg.index("em_tasks.spawn(")
    sync_path = seg[:task_call]
    for call in ("esphome.device_disconnected",
                 "em_ble_proxy.device_disconnected",
                 "device_gone", "notify_device_disconnected"):
        assert call not in sync_path, \
            f"{call} must move into the grace task, not run on close"
    assert "_release_device_services" in seg[task_call:], \
        "the close path must hand over to the grace task"


def test_the_grace_task_checks_for_a_replacement():
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index("async def _release_device_services")
    task = src[start:start + 2500]
    assert "CONTROL_RECONNECT_GRACE_S" in task
    assert "_devices.get(device.device_id)" in task, \
        "the task must check whether a replacement registered"
    for call in ("notify_device_disconnected", "esphome.device_disconnected",
                 "em_ble_proxy.device_disconnected", "device_gone"):
        assert call in task, f"{call} belongs in the deferred release"


def test_the_grace_window_exists_and_is_documented():
    src = (CONTROLLER / "em_controller.py").read_text()
    assert "CONTROL_RECONNECT_GRACE_S" in src
    # The data-plane constant this mirrors:
    assert "DATA_RECONNECT_GRACE_S = 3.0" in src


def test_the_stale_connection_guard_survives():
    """
    The 2026-07-14 guard solves a different ordering problem (close arriving
    AFTER a replacement registered) and must stay untouched.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    # the message wraps across two f-string lines — match the fragments
    assert "replacement is active" in src and "services up" in src, \
        "the out-of-order stale guard is still needed alongside the grace"


def _nodes(stmt):
    """Every AST node under stmt, skipping the list-valued fields."""
    yield stmt
    for _, val in ast.iter_fields(stmt):
        children = val if isinstance(val, list) else (val,)
        for node in children:
            if isinstance(node, ast.AST):
                yield from _nodes(node)


def _handle_data_body() -> list:
    """
    Every statement in handle_data, in source order, flattened out of the
    try/except it sits in.

    AST rather than a slice of the file's text. The first version of these two
    tests took 1,600 characters from `replaced = device.data_ws` and asserted
    the close fell inside, which is a statement about where the text sits: a
    single unrelated line inserted between the assignment and the close pushed
    it past the window and failed a build whose behaviour was correct. These
    ask what the handler does instead, and survive anything inserted around it.
    """
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    handler = next(n for n in _nodes(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_data")
    out: list = []

    def walk(stmts):
        for s in stmts:
            out.append(s)
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(s, attr, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    walk(inner)

    walk(handler.body)
    return out


def handler_body(flat: list):
    """The handler node itself, re-found for whole-function assertions.

    The flattened list is for ORDER comparisons; a "does this function mention
    X" check wants the real node, because _dump takes a node and the list is
    not one.
    """
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    return next(n for n in _nodes(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_data")


def _dump(stmt) -> str:
    """ast.dump that tolerates a statement node.

    ast.dump raises TypeError on the list-valued fields a statement carries
    (its handlers, orelse, finalbody) when reached through ast.walk, which is
    how these tests reach inside them. Flattening to a string of every node's
    dump sidesteps the traversal entirely.
    """
    return " ".join(ast.dump(n) for n in _nodes(stmt))


def _index_of(body: list, pred) -> int:
    for i, stmt in enumerate(body):
        if pred(stmt):
            return i
    raise AssertionError("expected statement not found in handle_data")


def _assigns_to(stmt, attr: str) -> bool:
    """`x.<attr> = ...` at statement level."""
    return (isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == attr
                    for t in stmt.targets))


def _closes_replaced(stmt) -> bool:
    """True for the `if` that guards the close, or a statement inside it.

    Matched on the AST rather than on source text: ast.dump renders
    `replaced.close()` as attr='close' under Name(id='replaced'), so a
    substring test for the dotted spelling never matches anything.
    """
    if isinstance(stmt, ast.If):
        return any(_closes_replaced(s) for s in stmt.body)
    # The close is an ARGUMENT to em_tasks.spawn, not the statement's own
    # call target, so a shape test on stmt.value.func alone finds nothing.
    # Collected by hand rather than with ast.walk, which raises on the list
    # fields (handlers, orelse) that a statement node carries.
    for node in _nodes(stmt):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "close"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "replaced"):
            return True
    return False




def _calls(stmt, attr: str) -> bool:
    """`x.<attr>(...)` at statement level — data_ready.set() and friends."""
    return (isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == attr)


def test_a_replacing_data_connection_closes_the_one_it_replaces():
    """
    #751: a device that reconnects registers the new socket before the
    controller notices the old one is dead, so the old one used to be
    abandoned rather than closed — it then lived until WS_PING_TIMEOUT_S
    reaped it, holding a handler task and a half-open TCP connection.
    """
    body = _handle_data_body()
    # The old socket is captured into a local ...
    capture = _index_of(body, lambda s: isinstance(s, ast.Assign)
                        and getattr(s.targets[0], "id", None) == "replaced")
    # ... BEFORE the assignment that overwrites it, or the socket to close is
    # the one that just arrived.
    install = _index_of(body, lambda s: _assigns_to(s, "data_ws")
                        and not (isinstance(s.targets[0], ast.Name)
                                 and s.targets[0].id == "replaced"))
    assert capture < install, (
        "the replaced socket must be captured before data_ws is reassigned, "
        "or the close fires on the new connection"
    )

    # The captured socket is closed, behind a guard that skips both the
    # first-connection case and the socket that just arrived. The guard is the
    # `if`; the spawn that does the closing sits in its body.
    # The `if` that guards it, not the spawn inside it — the spawn is an
    # Expr and has no `.test` to assert on.
    guards = [s for s in body if isinstance(s, ast.If) and _closes_replaced(s)]
    assert guards, "the replaced data socket is never closed"
    dump = _dump(guards[0].test)
    assert ("replaced" in dump and "IsNot" in dump and "None" in dump
            and "Constant(value=None)" in dump), (
        "the close must be guarded on the captured socket existing, got: "
        f"{dump}")
    assert ("IsNot" in dump and "Name(id='ws'" in dump), (
        "the close must be guarded on the captured socket being a DIFFERENT "
        f"one, got: {dump}")


def test_the_replaced_socket_is_closed_off_the_registration_path():
    """
    The peer is already beyond reach by the time we get here — a device only
    reconnects once its old socket is dead — so the close handshake cannot be
    waited on, and the new socket must be live before anything is awaited.
    """
    body = _handle_data_body()

    # The statement that does the closing, not an ancestor of it: the whole
    # handler body sits inside one `try`, and an index into that would put
    # the close before everything.
    close_stmt = _index_of(
        body, lambda s: isinstance(s, (ast.Expr, ast.If))
        and _closes_replaced(s))
    ready = _index_of(body, lambda s: _calls(s, "set")
                     and "data_ready" in _dump(s))

    # Spawned, not awaited inline: an `await` here stalls registration on a
    # peer that is never going to answer the handshake.
    assert "await" not in _dump(body[close_stmt]), (
        "the close must not be awaited on the registration path — a device "
        "only reconnects once its old socket is beyond reach"
    )
    assert "spawn" in _dump(body[close_stmt]).lower(), (
        "the close should be spawned so it cannot stall registration"
    )
    # The replacement is live first, so nothing about the working socket
    # waits on the dead one.
    assert ready < close_stmt, (
        "data_ready must be set before the replaced socket is reaped"
    )
    # A replacement must not abandon the playback session: a stream is
    # allowed to ride out the bounce on the new socket, and em_player
    # re-resolves the device per chunk to do it.
    assert "device_gone" not in _dump(handler_body(body)), (
        "a replacement must NOT abandon the playback session (#751 keeps the "
        "stream, it re-resolves the device per chunk instead)"
    )
