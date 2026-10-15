"""
Every HTTP route must be authenticated unless it is on a short, named list.

Two GET routes shipped with no decorator at all — `/api/devices/{id}/oww_assets`
(which opens a shell session on the device, holding the shell lock for up to
120s) and `/api/releases/controller` — and nothing noticed, because the only
guards were per-route checks written for routes someone had already thought
about. This one enumerates the routes from `create_app` instead, so a new
route without a decorator fails here rather than in the field.

Read with `ast`, not regexes over the text: a regex finds the words in a
comment explaining them and passes (see source-guards-match-their-own-prose).
"""

import ast

import _sources

# Reachable without a session, each for a reason:
PUBLIC = {
    "_serve_spa",           # the login page itself
    "_serve_dashboard",     # a static shell; every call it makes is authed
    "_redirect_root",
    "_get_setup_state",     # the login page asks whether first-run is pending
    "_post_setup",          # gated by the one-time bootstrap token
    "_post_login",
    "_post_ingress_login",  # gated by em_ingressauth.decide
    "_post_logout",         # a no-op without a session
}

# WebSocket upgrades cannot carry the Authorization header from a browser, so
# they authenticate inside the handler (see _ws_shell's docstring).
WS_SELF_AUTH = {"_ws_shell", "_ws_events"}

DECORATORS = {"require_auth", "require_admin"}


def _routes() -> dict[str, str]:
    """handler name -> path, for every app.router.add_<method>(path, handler)
    in any controller module.

    A registration this cannot read fails rather than being skipped: a route
    the guard does not see is a route nothing checks."""
    out = {}
    for module in _sources.MODULES:
        for node in ast.walk(_sources.tree(module)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr.startswith("add_")
                    and node.func.attr != "add_static"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "router"):
                continue
            where = f"{module.name}:{node.lineno}"
            assert len(node.args) == 2, f"{where}: unreadable route registration"
            handler = node.args[1]
            if isinstance(handler, ast.Attribute):     # module.handler
                handler = ast.Name(handler.attr)
            assert isinstance(handler, ast.Name), \
                f"{where}: route handler is not a plain name"
            path = node.args[0].value if isinstance(node.args[0], ast.Constant) else "?"
            out[handler.id] = path
    return out


def _functions() -> dict[str, ast.AsyncFunctionDef]:
    """Every route handler, by name, from whichever module defines it. A name
    defined twice would make the route table ambiguous, so that fails."""
    defs = {}
    for module in _sources.MODULES:
        for n in _sources.tree(module).body:
            if isinstance(n, ast.AsyncFunctionDef):
                defs.setdefault(n.name, []).append((module.name, n))
    out = {}
    for name in _routes():
        found = defs.get(name, [])
        assert len(found) <= 1, \
            f"route handler {name} is defined in {[m for m, _ in found]}"
        if found:
            out[name] = found[0][1]
    return out


def _decorators(fn) -> set[str]:
    return {d.attr for d in fn.decorator_list if isinstance(d, ast.Attribute)}


def _calls(fn, attr: str) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == attr for n in ast.walk(fn))


def test_the_route_table_was_found():
    # Guards the guard: a refactor of create_app that this parser no longer
    # understands must fail loudly, not pass on an empty table.
    routes = _routes()
    assert len(routes) > 50
    assert "_get_devices" in routes


def test_every_route_is_authenticated_or_named_public():
    fns = _functions()
    missing = []
    for name, path in sorted(_routes().items()):
        assert name in fns, f"route {path} -> {name}: handler not found"
        if name in PUBLIC or name in WS_SELF_AUTH:
            continue
        if not (_decorators(fns[name]) & DECORATORS):
            missing.append(f"{path} ({name})")
    assert not missing, (
        "routes with no @auth.require_auth / @auth.require_admin: "
        + ", ".join(missing)
        + ". Add the decorator, or add the handler to PUBLIC with the reason.")


def test_websocket_handlers_authenticate_themselves():
    fns = _functions()
    for name in WS_SELF_AUTH:
        assert _calls(fns[name], "ws_resolve_session"), (
            f"{name} is exempt from the decorator rule only because it calls "
            f"auth.ws_resolve_session itself")


def test_public_list_names_real_routes():
    # A stale entry is an exemption waiting for a new handler to inherit it.
    routes = _routes()
    assert not (PUBLIC | WS_SELF_AUTH) - set(routes)


def test_oww_assets_and_controller_release_need_a_session():
    fns = _functions()
    assert _decorators(fns["_get_oww_assets"]) & DECORATORS
    assert _decorators(fns["_get_controller_release"]) & DECORATORS
