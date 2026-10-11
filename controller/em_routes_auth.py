"""
Sign-in, sessions and accounts: password, Home Assistant ingress and OpenID
Connect login, logout, the current user, changing a password, and user roles.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from typing import Optional

from aiohttp import web

import em_auth as auth
import em_db as db
import em_http
import em_ingressauth
import em_oidc

log = logging.getLogger("echomuse.api")


# ─── OpenID Connect sign-in ───────────────────────────────────────────────────
# Configured from the environment (see em_oidc). None means the dashboard is
# password-only, exactly as before. A half-configured install is logged and
# also left password-only, so a typo never locks the operator out of the one
# way in they still have.
def _load_oidc_config() -> Optional[em_oidc.OidcConfig]:
    try:
        return em_oidc.config_from_env(os.environ)
    except em_oidc.ConfigError as exc:
        log.error(f"[oidc] disabled — configuration is unusable: {exc}")
        return None


_oidc_cfg: Optional[em_oidc.OidcConfig] = _load_oidc_config()
# A sign-in started (state -> verifier, nonce), and a finished one waiting for
# the browser to collect it (code -> identity, role). Memory-only and bounded.
_oidc_flows = em_oidc.OneShotStore(em_oidc.FLOW_TTL_S)
_oidc_exchanges = em_oidc.OneShotStore(em_oidc.EXCHANGE_TTL_S)
# Discovery is cached so a sign-in does not cost an extra round trip, and
# re-fetched hourly so a provider that rotates endpoints is followed.
OIDC_DISCOVERY_TTL_S = 3600
_oidc_discovery_cache: Optional[tuple[em_oidc.Endpoints, float]] = None


def register(app: web.Application) -> None:
    app.router.add_post("/api/auth/login",           _post_login)
    # Public: decides for itself whether the request is a genuine ingress
    # request. Requiring a session here would defeat the purpose.
    app.router.add_post("/api/auth/ingress",          _post_ingress_login)
    app.router.add_post("/api/auth/logout",          _post_logout)
    # OpenID Connect. All four are public by nature — they are how a session
    # is obtained — and each decides for itself: login and callback are
    # bound together by the flow cookie and the state, exchange by a
    # single-use code that only a completed callback can have issued.
    app.router.add_get("/api/auth/oidc/config",      _get_oidc_config)
    app.router.add_get("/api/auth/oidc/login",       _get_oidc_login)
    app.router.add_get("/api/auth/oidc/callback",    _get_oidc_callback)
    app.router.add_post("/api/auth/oidc/exchange",   _post_oidc_exchange)
    app.router.add_get("/api/auth/me",               _get_me)
    app.router.add_post("/api/auth/change-password", _post_change_password)

    # Users — roles. There was no way to change one at all until 2026-08-14,
    # which only became load-bearing when ingress started provisioning
    # accounts the operator never created.
    app.router.add_get("/api/users",        _get_users)
    app.router.add_patch("/api/users/{id}", _patch_user)


@auth.require_admin
async def _get_users(request: web.Request) -> web.Response:
    """
    GET /api/users — accounts and their roles. ADMIN ONLY.

    Never returns password_hash. `ha_linked` says whether Home Assistant
    governs this account's role, which is what makes a refused PATCH
    explicable rather than arbitrary.
    """
    loop  = asyncio.get_event_loop()
    users = await loop.run_in_executor(None, db.get_all_users)
    return em_http.ok([{
        "id":         u["id"],
        "username":   u["username"],
        "role":       u["role"],
        "ha_linked":  bool(u["ha_user_id"]),
        "oidc_linked": bool(u["oidc_subject"]),
        "created_at": u["created_at"],
    } for u in users])


@auth.require_admin
async def _patch_user(request: web.Request) -> web.Response:
    """
    PATCH /api/users/{id}  {role} — change a user's role. ADMIN ONLY.

    This is the ONLY way to promote someone, including accounts Home
    Assistant created through ingress — roles are not mirrored from HA, so
    nothing here is later overwritten by a login.

    One refusal: **never leave the install with no admin.** On the standalone
    container local accounts are the only auth, so an install with no admin
    has no way back in — and this endpoint is the one an admin reaches for
    while tidying up.
    """
    body = await em_http.json_body(request)
    role = em_http.require_str(body, "role")
    if role not in ("admin", "readonly"):
        return em_http.error("bad_request", "role must be 'admin' or 'readonly'", 400)

    try:
        user_id = int(request.match_info["id"])
    except ValueError:
        return em_http.error("bad_request", "user id must be numeric", 400)

    loop = asyncio.get_event_loop()
    user = await loop.run_in_executor(None, db.get_user_by_id, user_id)
    if user is None:
        return em_http.error("user_not_found", f"No user: {user_id}", 404)

    if user["oidc_subject"]:
        # The provider's groups decide this role at every sign-in, so an edit
        # here would be overwritten at that person's next login.
        return em_http.error(
            "oidc_linked",
            "This account's role comes from its group membership at the "
            "identity provider; change the group there", 409)

    if user["role"] == role:
        return em_http.ok({"id": user_id, "role": role, "changed": False})

    if user["role"] == "admin":
        admins = await loop.run_in_executor(None, db.admin_count)
        if admins <= 1:
            return em_http.error(
                "last_admin",
                "This is the only admin — promote someone else first", 409)

    await loop.run_in_executor(None, db.set_user_role, user_id, role)
    log.info(f"[api] {request['user']['username']} set "
             f"{user['username']} to {role}")
    return em_http.ok({"id": user_id, "role": role, "changed": True})


async def _post_ingress_login(request: web.Request) -> web.Response:
    """
    POST /api/auth/ingress — authenticate as the Home Assistant user that
    Supervisor forwarded. → {token, role} or 401.

    Home Assistant has already authenticated this person; a second EchoMuse
    password would be a lock on a door that is already locked. Supervisor
    strips client-supplied copies of these headers before proxying, so their
    presence on a request that genuinely came from the gateway is proof of
    an authenticated HA session.

    Whether it *did* come from the gateway is em_ingressauth.decide's
    judgement, made from the deployment mode and the peer address together.
    A 401 here is not a failure — it is the ordinary answer everywhere that
    is not the add-on, and the dashboard falls back to the login form.
    """
    identity = em_ingressauth.decide(
        ingress_only=em_ingressauth.INGRESS_ONLY,
        remote=request.remote,
        user_id=request.headers.get("X-Remote-User-Id"),
        username=request.headers.get("X-Remote-User-Name"),
        display_name=request.headers.get("X-Remote-User-Display-Name"),
    )
    if identity is None:
        return em_http.error("not_authenticated",
                      "Home Assistant authentication is not available", 401)

    token, role = await auth.login_via_ingress(identity)
    return em_http.ok({"token": token, "role": role, "via": "ingress"})


async def _get_oidc_config(request: web.Request) -> web.Response:
    """GET /api/auth/oidc/config — what the login page needs to draw itself.
    Public, and carries no secret: not even the issuer."""
    cfg = _oidc_cfg
    if cfg is None:
        return em_http.ok({"enabled": False, "password_login": True})
    return em_http.ok({"enabled": True, "label": cfg.button_label,
                       "password_login": cfg.password_login})


async def _oidc_endpoints() -> em_oidc.Endpoints:
    global _oidc_discovery_cache
    now = time.monotonic()
    if _oidc_discovery_cache and _oidc_discovery_cache[1] > now:
        return _oidc_discovery_cache[0]
    endpoints = await em_oidc.discover(_oidc_cfg)
    _oidc_discovery_cache = (endpoints, now + OIDC_DISCOVERY_TTL_S)
    return endpoints


def _oidc_redirect(location: str, *, clear_cookie: bool = False) -> web.Response:
    resp = web.Response(status=302, headers={
        "Location": location, "Cache-Control": "no-store"})
    if clear_cookie:
        name, path, secure = em_oidc.flow_cookie(_oidc_cfg)
        resp.del_cookie(name, path=path, secure=secure, httponly=True,
                        samesite="Lax")
    return resp


def _oidc_fail(reason: str) -> web.Response:
    """Send the browser home with a FIXED error code. Nothing the provider or
    the client supplied is ever reflected into the page."""
    return _oidc_redirect(f"{_oidc_cfg.landing_url}?oidc_error={reason}",
                          clear_cookie=True)


async def _get_oidc_login(request: web.Request) -> web.Response:
    """GET /api/auth/oidc/login — start a sign-in: 302 to the provider."""
    cfg = _oidc_cfg
    if cfg is None:
        return em_http.error("oidc_disabled", "Single sign-on is not configured", 404)
    try:
        endpoints = await _oidc_endpoints()
    except em_oidc.OidcError as exc:
        log.warning(f"[oidc] provider unreachable: {exc}")
        return em_http.error("oidc_unavailable",
                             "Single sign-on is unavailable right now", 503)

    state, nonce = em_oidc.random_token(), em_oidc.random_token()
    verifier, challenge = em_oidc.new_pkce()
    _oidc_flows.put(state, {"verifier": verifier, "nonce": nonce})

    resp = _oidc_redirect(em_oidc.authorization_url(
        endpoints.authorization_endpoint, cfg,
        state=state, nonce=nonce, challenge=challenge))
    # Binds the callback to THIS browser, so a link sent to someone else
    # cannot sign them in as the sender (login CSRF). Lax, not Strict: the
    # callback is a top-level navigation arriving from another site.
    name, path, secure = em_oidc.flow_cookie(cfg)
    resp.set_cookie(name, state, max_age=em_oidc.FLOW_TTL_S, path=path,
                    httponly=True, samesite="Lax", secure=secure)
    return resp


async def _get_oidc_callback(request: web.Request) -> web.Response:
    """
    GET /api/auth/oidc/callback — the provider sends the browser back here.

    Success hands the browser a one-shot code in the URL FRAGMENT, which is
    never sent to a server or logged; the dashboard swaps it for a session
    with a POST. Every failure is a redirect with a fixed code.
    """
    cfg = _oidc_cfg
    if cfg is None:
        return em_http.error("oidc_disabled", "Single sign-on is not configured", 404)

    state = request.query.get("state", "")
    cookie = request.cookies.get(em_oidc.flow_cookie(cfg)[0], "")
    # Popped before anything else is checked, so a state is spent whether the
    # attempt succeeds or not.
    flow = _oidc_flows.pop(state) if state else None
    if flow is None or not cookie or not secrets.compare_digest(
            cookie.encode(), state.encode()):
        return _oidc_fail("expired")

    if request.query.get("error"):
        log.info(f"[oidc] provider returned error "
                 f"{request.query.get('error')[:40]!r}")
        return _oidc_fail("denied")
    code = request.query.get("code", "")
    if not code:
        return _oidc_fail("failed")

    try:
        endpoints = await _oidc_endpoints()
        claims = await em_oidc.exchange_code(
            cfg, endpoints, code=code, verifier=flow["verifier"])
        identity = em_oidc.validate_claims(
            claims, cfg, nonce=flow["nonce"], now=time.time())
    except em_oidc.OidcError as exc:
        log.warning(f"[oidc] sign-in failed: {exc}")
        return _oidc_fail("failed")

    role = em_oidc.role_for_groups(identity.groups, cfg)
    if role is None:
        # login_via_oidc refuses, and on the way ends any sessions this
        # person already holds — losing the group is what revokes access.
        with contextlib.suppress(auth.AuthError):
            await auth.login_via_oidc(identity, None,
                                      session_hours=cfg.session_hours)
        return _oidc_fail("no_access")

    exchange_code = em_oidc.random_token()
    _oidc_exchanges.put(exchange_code, (identity, role))
    return _oidc_redirect(f"{cfg.landing_url}#oidc={exchange_code}",
                          clear_cookie=True)


async def _post_oidc_exchange(request: web.Request) -> web.Response:
    """POST /api/auth/oidc/exchange — {code} → {token, role, via}. The code is
    single-use and only a completed callback can have issued one."""
    cfg = _oidc_cfg
    if cfg is None:
        return em_http.error("oidc_disabled", "Single sign-on is not configured", 404)
    body = await em_http.json_body(request)
    entry = _oidc_exchanges.pop(em_http.require_str(body, "code"))
    if entry is None:
        return em_http.error("invalid_code", "Sign-in expired — please try again", 401)
    identity, role = entry
    token, role = await auth.login_via_oidc(
        identity, role, session_hours=cfg.session_hours)
    return em_http.ok({"token": token, "role": role, "via": "oidc"})


async def _post_login(request: web.Request) -> web.Response:
    """POST /api/auth/login — {username, password} → {token, role}"""
    if _oidc_cfg is not None and not _oidc_cfg.password_login:
        return em_http.error("password_login_disabled",
                             "Password sign-in is turned off; use single sign-on", 403)
    body     = await em_http.json_body(request)
    username = em_http.require_str(body, "username")
    password = em_http.require_str(body, "password")

    token, role = await auth.login(username, password)
    return em_http.ok({"token": token, "role": role})


async def _post_logout(request: web.Request) -> web.Response:
    """POST /api/auth/logout — invalidate current session."""
    user = await auth.resolve_session(request)
    if user:
        await auth.logout(user["token"])
    return em_http.ok({})


@auth.require_auth
async def _get_me(request: web.Request) -> web.Response:
    """GET /api/auth/me — current user info."""
    user = request["user"]
    return em_http.ok({
        "id":       user["id"],
        "username": user["username"],
        "role":     user["role"],
    })


@auth.require_auth
async def _post_change_password(request: web.Request) -> web.Response:
    """
    POST /api/auth/change-password

    Body: {current_password, new_password}
    Any authenticated user can change their own password.
    Verifies current password before accepting the new one.
    """
    user = request["user"]
    body = await em_http.json_body(request)
    current_password = em_http.require_str(body, "current_password")
    new_password     = em_http.require_str(body, "new_password")

    if len(new_password) < 8:
        return em_http.error("invalid_input", "New password must be at least 8 characters", 400)

    loop = asyncio.get_event_loop()
    db_user = await loop.run_in_executor(None, db.get_user_by_id, user["id"])
    if db_user is None:
        return em_http.error("user_not_found", "User not found", 404)

    if not await auth.verify_password_async(current_password, db_user["password_hash"]):
        return em_http.error("invalid_credentials", "Current password is incorrect", 401)

    new_hash = await auth.hash_password_async(new_password)
    revoked = await loop.run_in_executor(
        None, lambda: db.update_user_password(
            user["id"], new_hash, keep_session=user["token"]))
    log.info(f"[api] Password changed for user: {user['username']}")
    return em_http.ok({"ok": True, "sessions_ended": revoked})
