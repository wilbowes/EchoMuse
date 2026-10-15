"""
Sign-in, sessions and accounts: password and Home Assistant ingress login,
logout, the current user, changing a password, and user roles.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

import em_auth as auth
import em_db as db
import em_http
import em_ingressauth

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    app.router.add_post("/api/auth/login",           _post_login)
    # Public: decides for itself whether the request is a genuine ingress
    # request. Requiring a session here would defeat the purpose.
    app.router.add_post("/api/auth/ingress",          _post_ingress_login)
    app.router.add_post("/api/auth/logout",          _post_logout)
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


async def _post_login(request: web.Request) -> web.Response:
    """POST /api/auth/login — {username, password} → {token, role}"""
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
