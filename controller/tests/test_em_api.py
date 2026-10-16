"""
em_api: the assembly, middlewares, session pruning, supervisor log fetch,
and notify_* hooks em_controller calls.

The route modules are tested in their own files (test_route_resolution.py
walks the assembled app to make sure every URL lands at its own handler);
this file is for the small surface that lives in em_api itself: the
middlewares, the background tasks, the supervisor-log fetch on reconnect,
and the four notify_* functions em_controller calls when a device
connects, disconnects, asks to pair, or appears unapproved.
"""

import asyncio


# ─── create_app / create_runner ──────────────────────────────────────────────

def test_create_app_registers_every_route_module():
    """Every em_routes_* module's register() is called, so /api/* is live.

    test_route_resolution.py goes deeper (every URL lands at its own
    handler) — this is the smoke test that the assembly itself ran.
    """
    import em_api
    app = asyncio.run(em_api.create_app())
    routes = [r.method + " " + r.resource.canonical
              for r in app.router.routes()]
    # A few of the most consequential URLs, picked because they are
    # what a user hits first on the dashboard.
    assert "GET /" in routes
    assert "GET /dashboard" in routes
    assert "POST /api/auth/login" in routes
    assert "GET /api/devices" in routes


def test_create_runner_wraps_the_app_in_an_apprunner():
    """em_controller's main() awaits this; it must return a real AppRunner."""
    import em_api
    from aiohttp import web
    runner = asyncio.run(em_api.create_runner())
    assert isinstance(runner, web.AppRunner)


# ─── Ingress middleware ──────────────────────────────────────────────────────

def test_ingress_middleware_is_a_noop_outside_addon(monkeypatch):
    """Without INGRESS_ONLY, every request reaches the handler."""
    import em_api
    import em_ingressauth
    monkeypatch.setattr(em_ingressauth, "INGRESS_ONLY", False)
    called = []
    handler = _Handler(called, 200, "ok")
    mw_req = _req(remote="10.0.0.5")
    resp = asyncio.run(em_api._ingress_only_middleware(mw_req, handler.call))
    assert resp.status == 200
    assert len(called) == 1


def test_ingress_middleware_blocks_a_non_gateway_peer_in_addon_mode(monkeypatch):
    """In the add-on, only the HA ingress gateway may reach the dashboard.

    A request whose peer is not the configured gateway must get a 403
    from the middleware before the handler ever runs.
    """
    from aiohttp.web import HTTPForbidden
    import em_api
    import em_ingressauth
    # make_mocked_request's `remote` is None in current aiohttp, so we
    # set the gateway to a non-None value to force the `remote != gateway`
    # branch.
    monkeypatch.setattr(em_ingressauth, "INGRESS_ONLY", True)
    monkeypatch.setattr(em_ingressauth, "INGRESS_GATEWAY_IP", "172.30.32.2")
    handler = _Handler([], 200, "ok")
    mw_req = _req()
    try:
        asyncio.run(em_api._ingress_only_middleware(mw_req, handler.call))
    except HTTPForbidden:
        return
    raise AssertionError("expected 403 for non-gateway peer in add-on mode")


def test_ingress_middleware_lets_the_gateway_through(monkeypatch):
    """The gateway itself must reach the handler — the middleware is a guard,
    not a wall."""
    import em_api
    import em_ingressauth
    # make_mocked_request's `remote` is None in current aiohttp, so we
    # align both sides: a None remote from the gateway IP is treated as
    # the gateway itself.
    monkeypatch.setattr(em_ingressauth, "INGRESS_ONLY", True)
    monkeypatch.setattr(em_ingressauth, "INGRESS_GATEWAY_IP", None)
    called = []
    handler = _Handler(called, 200, "ok")
    mw_req = _req()
    resp = asyncio.run(em_api._ingress_only_middleware(mw_req, handler.call))
    assert resp.status == 200
    assert len(called) == 1


# ─── Error middleware ────────────────────────────────────────────────────────

def test_error_middleware_reraises_http_exceptions_so_aiohttp_handles_them():
    """web.HTTPException is aiohttp's own — it carries its status and body,
    and re-raising lets aiohttp's default handler do the right thing.
    Swallowing it as a generic 500 would lose the status code."""
    from aiohttp.web import HTTPNotFound
    import em_api

    def handler(req):
        raise HTTPNotFound(text="nope")

    mw_req = _req()
    try:
        asyncio.run(em_api._error_middleware(mw_req, handler))
    except HTTPNotFound as e:
        assert e.text == "nope"
        return
    raise AssertionError("expected HTTPNotFound to propagate")


def test_error_middleware_converts_an_auth_error_to_its_response():
    """em_auth.AuthError already carries the response body; the middleware
    catches it first because the AuthError exception path is the dashboard's
    most common login flow and a stack trace there is noise."""
    import em_api
    import em_auth

    class _AuthErr(em_auth.AuthError):
        def __init__(self):
            super().__init__("nope", 401)
        def to_response(self):
            from aiohttp import web
            return web.json_response({"error": "nope", "code": "unauth"}, status=401)

    def handler(req):
        raise _AuthErr()

    mw_req = _req()
    resp = asyncio.run(em_api._error_middleware(mw_req, handler))
    assert resp.status == 401
    assert "nope" in resp.text


def test_error_middleware_returns_a_uniform_500_for_unexpected_errors():
    """Anything else is a 500 with the same body shape. The dashboard reads
    `code` to decide what to show, so a handler that returns a 500 with a
    different body is a bug."""
    import em_api
    import json
    def handler(req):
        raise RuntimeError("boom")
    mw_req = _req(method="GET", path="/api/test")
    resp = asyncio.run(em_api._error_middleware(mw_req, handler))
    assert resp.status == 500
    body = json.loads(resp.text)
    assert body["code"] == "internal_error"


# ─── session_prune_loop ──────────────────────────────────────────────────────

def test_session_prune_runs_prune_sessions(monkeypatch):
    """The prune must call db.prune_sessions and keep going on a clean run."""
    import em_api
    import em_db as db
    calls = []
    monkeypatch.setattr(db, "prune_sessions", lambda: calls.append(1) or 1)
    # Let the first sleep complete (a no-op) so the prune runs, then
    # stop the loop on the next sleep.
    sleeps = []
    async def fake_sleep(_):
        sleeps.append(_)
        if len(sleeps) >= 2:
            raise _Stop()
    monkeypatch.setattr(em_api.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(em_api.session_prune_loop())
    except _Stop:
        pass
    assert calls == [1]
    assert sleeps == [3600, 3600]


def test_session_prune_logs_and_continues_when_prune_raises(monkeypatch):
    """A bad prune must not stop the loop — the controller needs the next
    hourly prune to run, not to be silently dead."""
    import em_api
    import em_db as db
    state = {"n": 0}
    def boom():
        state["n"] += 1
        if state["n"] >= 2:
            raise _Stop()
        raise RuntimeError("db locked")
    monkeypatch.setattr(db, "prune_sessions", boom)
    sleeps = []
    async def fake_sleep(_):
        sleeps.append(_)
        if state["n"] >= 2:
            raise _Stop()
    monkeypatch.setattr(em_api.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(em_api.session_prune_loop())
    except _Stop:
        pass
    assert state["n"] == 2


# ─── supervisor log fetch ────────────────────────────────────────────────────

def test_collect_supervisor_log_does_nothing_when_device_is_offline():
    """em_controller calls this on every reconnect; an offline device has
    no live shell plane, and the function must not raise."""
    import em_api
    import em_state
    em_state.devices.clear()
    asyncio.run(em_api._collect_supervisor_log("dev1"))


def test_collect_supervisor_log_pushes_a_warning_when_there_is_no_log(monkeypatch):
    """A device that has never run a failed update (or predates the
    persistent log) returns empty output, and we say so."""
    import em_api
    import em_state
    em_state.devices["dev1"] = _Live()

    async def fake_run(live, cmd, timeout):
        return ""
    monkeypatch.setattr(em_api.em_shell, "run", fake_run)

    pushed = []
    async def fake_push_log(*a, **k):
        pushed.append((a, k))
    monkeypatch.setattr(em_api.em_events, "push_log", fake_push_log)
    asyncio.run(em_api._collect_supervisor_log("dev1"))
    assert len(pushed) == 1
    device_id, level, source, msg = pushed[0][0]
    assert device_id == "dev1"
    assert level == "warn"
    assert source == "controller"
    assert "no supervisor log" in msg


def test_collect_supervisor_log_pushes_the_log_when_it_exists(monkeypatch):
    """The whole point of this function: when the device has a log, push
    it into the dashboard's log stream where somebody can read it."""
    import em_api
    import em_state
    em_state.devices["dev1"] = _Live()

    async def fake_run(live, cmd, timeout):
        return "boot: retrying\nboot: failed\n"
    monkeypatch.setattr(em_api.em_shell, "run", fake_run)

    pushed = []
    async def fake_push_log(*a, **k):
        pushed.append((a, k))
    monkeypatch.setattr(em_api.em_events, "push_log", fake_push_log)
    asyncio.run(em_api._collect_supervisor_log("dev1"))
    assert len(pushed) == 1
    device_id, level, source, msg = pushed[0][0]
    assert device_id == "dev1"
    assert level == "warn"
    assert "boot: retrying" in msg
    assert "boot: failed" in msg


# ─── notify_device_connected ────────────────────────────────────────────────

def test_notify_device_connected_uses_passed_version_without_db(monkeypatch):
    """The device handshake carries the version. em_controller passes it
    directly; a DB lookup must NOT happen on the hot path."""
    import em_api
    import em_state
    em_state.devices.clear()
    pushed = []

    async def fake_push(e):
        pushed.append(e)
    monkeypatch.setattr(em_api.em_events, "push", fake_push)

    def must_not_run(*a, **k):
        raise AssertionError("db.get_device must not be called")
    monkeypatch.setattr(em_api.db, "get_device", must_not_run)

    asyncio.run(em_api.notify_device_connected("dev1", version="v2.18.0"))
    assert pushed[0]["firmware_ver"] == "v2.18.0"
    assert pushed[0]["type"] == "device_connected"


def test_notify_device_connected_falls_back_to_db_when_version_omitted(monkeypatch, tmp_path):
    """Older callers may not pass version; the function then looks it up
    so the dashboard's device cache stays in step on reconnect."""
    import em_api
    import em_state
    em_state.devices.clear()
    pushed = []

    async def fake_push(e):
        pushed.append(e)
    monkeypatch.setattr(em_api.em_events, "push", fake_push)

    # A unique DB path per test invocation, so any test that has run
    # before cannot collide on UNIQUE.
    import em_db
    db_path = str(tmp_path / f"test_fallback_{id(pushed)}.db")
    em_db.init(db_path)
    em_db.register_new_device("test_fallback_dev1", "1.2.3.4", "v2.17.0")

    asyncio.run(em_api.notify_device_connected("test_fallback_dev1"))
    assert pushed[0]["firmware_ver"] == "v2.17.0"


def test_notify_device_connected_schedules_supervisor_log_fetch(monkeypatch):
    """A device that owed an explanation from a previous failed update
    should have it collected on reconnect — the set is checked and the
    task is spawned."""
    import em_api
    import em_state
    import em_ota
    em_state.devices.clear()
    em_ota.supervisor_log_wanted.add("dev1")
    pushed = []

    async def fake_push(e):
        pushed.append(e)
    monkeypatch.setattr(em_api.em_events, "push", fake_push)

    spawned = []
    def fake_spawn(coro):
        spawned.append(coro)
        coro.close()
    monkeypatch.setattr(em_api.em_tasks, "spawn", fake_spawn)

    asyncio.run(em_api.notify_device_connected("dev1", version="v2.18.0"))
    assert "dev1" not in em_ota.supervisor_log_wanted
    assert len(spawned) == 1


def test_notify_device_connected_skips_supervisor_fetch_when_not_owed(monkeypatch):
    """The fetch is conditional on the set. A clean connect does not
    queue a fetch."""
    import em_api
    import em_state
    import em_ota
    em_state.devices.clear()
    em_ota.supervisor_log_wanted.discard("dev1")
    pushed = []

    async def fake_push(e):
        pushed.append(e)
    monkeypatch.setattr(em_api.em_events, "push", fake_push)

    spawned = []
    def fake_spawn(coro):
        spawned.append(coro)
        coro.close()
    monkeypatch.setattr(em_api.em_tasks, "spawn", fake_spawn)

    asyncio.run(em_api.notify_device_connected("dev1", version="v2.18.0"))
    assert spawned == []


# ─── notify_device_disconnected ────────────────────────────────────────────

def test_notify_device_disconnected_pushes_an_event(monkeypatch):
    """em_controller calls this when a device drops; the dashboard's
    device list refreshes on it."""
    import em_api
    import em_events
    pushed = []
    async def fake_push(e):
        pushed.append(e)
    monkeypatch.setattr(em_events, "push", fake_push)
    asyncio.run(em_api.notify_device_disconnected("dev1"))
    assert pushed[0] == {"type": "device_disconnected", "device_id": "dev1"}


# ─── notify_pair_request ────────────────────────────────────────────────────

def test_notify_pair_request_pushes_only_when_em_pairing_accepts(monkeypatch):
    """em_pairing is the policy: it may decide not to ask for an
    approval (already paired, role mismatch, etc.). The hook respects
    that — no event is pushed when the policy says no."""
    import em_api
    pushed = []
    async def fake_push(e):
        pushed.append(e)
    monkeypatch.setattr(em_api.em_events, "push", fake_push)
    monkeypatch.setattr(em_api.em_pairing, "request", lambda *a, **k: False)
    asyncio.run(em_api.notify_pair_request("dev1", "link"))
    assert pushed == []


def test_notify_pair_request_pushes_a_log_and_an_event_when_accepted(monkeypatch):
    """On a real pair request, the dashboard gets both a log line and
    the event that drives the Approve button."""
    import em_api
    pushed = []
    logged = []
    async def fake_push(e):
        pushed.append(e)
    async def fake_push_log(*a, **k):
        logged.append(a)
    monkeypatch.setattr(em_api.em_events, "push", fake_push)
    monkeypatch.setattr(em_api.em_events, "push_log", fake_push_log)
    monkeypatch.setattr(em_api.em_pairing, "request", lambda *a, **k: True)
    asyncio.run(em_api.notify_pair_request("dev1", "link"))
    assert any(e["type"] == "device_pair_request" for e in pushed)
    # push_log(device_id, level, source, message) — 4 positional args.
    assert any(len(a) == 4 and "approve" in a[3].lower() for a in logged)


# ─── notify_device_pending ──────────────────────────────────────────────────

def test_notify_device_pending_pushes_the_ip_with_the_event(monkeypatch):
    """The dashboard shows pending devices by IP — the field is for the
    admin to know which physical device is asking for approval."""
    import em_api
    pushed = []
    async def fake_push(e):
        pushed.append(e)
    monkeypatch.setattr(em_api.em_events, "push", fake_push)
    asyncio.run(em_api.notify_device_pending("dev1", "10.0.0.42"))
    assert pushed[0] == {
        "type": "device_pending",
        "device_id": "dev1",
        "ip": "10.0.0.42",
    }


# ─── SUPERVISOR_LOG constant ─────────────────────────────────────────────────

def test_supervisor_log_path_matches_start_server_sh():
    """The script writes to a path; the controller reads from the same one.
    A drift here is the kind of bug that costs an evening of debugging
    an "undiagnosable" failed update — both halves must agree, byte for
    byte. Pinned by tests/test_deploy.py; this is the local assertion
    that the constant is still a constant."""
    from pathlib import Path
    import em_api
    script = (Path(__file__).resolve().parents[1]
              / "device_payloads" / "start_server.sh").read_text()
    import re
    m = re.search(r"^SUP_LOG=(\S+)", script, re.M)
    assert m, "start_server.sh no longer defines SUP_LOG"
    assert em_api.SUPERVISOR_LOG == m.group(1)


# ─── helpers ────────────────────────────────────────────────────────────────

class _Stop(Exception):
    pass


async def _noop_coro():
    return None


class _Handler:
    """A middleware test handler that records calls and returns a canned response."""
    def __init__(self, called, status, text):
        self.called = called
        self.status = status
        self.text = text
    async def call(self, req):
        self.called.append(req)
        from aiohttp import web
        return web.Response(status=self.status, text=self.text)


def _req(remote="127.0.0.1", method="GET", path="/"):
    from aiohttp.test_utils import make_mocked_request
    # make_mocked_request has no `remote` arg in current aiohttp; the
    # middleware reads request.remote which is None by default. The
    # remote value is just here to make the test's intent obvious.
    del remote
    return make_mocked_request(method, path)


class _Live:
    """A device object that em_shell.run accepts; we mock the call anyway."""
    def __init__(self):
        self.device_id = "dev1"