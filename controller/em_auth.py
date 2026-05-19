"""
auth.py — EchoMuse Controller authentication layer
===================================================

Handles password hashing, session token generation, login/logout,
and role-based access control decorators for aiohttp route handlers.

Roles:
    admin    — full access: approve devices, push config/binaries,
               shell access, manage users, change system config
    readonly — read-only: view fleet state, logs, config

Usage:
    from auth import require_auth, require_admin, login, logout

    # In an aiohttp route handler:
    @require_admin
    async def approve_device(request):
        user = request["user"]   # injected by decorator
        ...

    # Login:
    token, role = await login(username, password)

    # Logout:
    await logout(token)
"""

import asyncio
import logging
import os
import secrets
from functools import wraps
from typing import Optional

import bcrypt
from aiohttp import web

import em_db as db

log = logging.getLogger("echomuse.auth")

# ─── Constants ────────────────────────────────────────────────────────────────

# Token length in bytes — 32 bytes = 64 hex chars, 256 bits of entropy.
TOKEN_BYTES = 32

# bcrypt work factor. 12 is a reasonable default for 2024 hardware.
# Increase if hardware allows; don't decrease below 10.
BCRYPT_ROUNDS = 12

# Header and cookie name for session token.
AUTH_HEADER = "Authorization"
AUTH_COOKIE = "session"

# ─── Password hashing ────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """
    Return a bcrypt hash of password.

    This is intentionally synchronous and slow — callers on the asyncio
    hot path must use hash_password_async() instead.
    """
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
    ).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """
    Return True if password matches the bcrypt hash.

    Intentionally synchronous — callers on the asyncio hot path must
    use verify_password_async() instead.
    """
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            hashed.encode("utf-8"),
        )
    except Exception:
        return False


async def hash_password_async(password: str) -> str:
    """Non-blocking wrapper — runs bcrypt in the default thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, hash_password, password)


async def verify_password_async(password: str, hashed: str) -> bool:
    """Non-blocking wrapper — runs bcrypt in the default thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, verify_password, password, hashed)


# ─── Token generation ─────────────────────────────────────────────────────────

def generate_token() -> str:
    """Return a cryptographically random hex token."""
    return secrets.token_hex(TOKEN_BYTES)


# ─── Login / logout ───────────────────────────────────────────────────────────

async def login(username: str, password: str) -> tuple[str, str]:
    """
    Validate credentials and create a session.

    Returns (token, role) on success.
    Raises AuthError on failure — do not distinguish between "user not
    found" and "wrong password" to avoid user enumeration.
    """
    loop = asyncio.get_event_loop()

    user = await loop.run_in_executor(None, db.get_user_by_username, username)
    if user is None:
        # Run a dummy bcrypt check to prevent timing-based user enumeration.
        await verify_password_async(password, _DUMMY_HASH)
        raise AuthError("invalid_credentials", "Invalid username or password", 401)

    if not await verify_password_async(password, user["password_hash"]):
        raise AuthError("invalid_credentials", "Invalid username or password", 401)

    token = generate_token()
    expiry_days = int(db.get_config("session_expiry_days", "30") or 30)

    await loop.run_in_executor(
        None, db.create_session, token, user["id"], expiry_days
    )
    log.info(f"[auth] Login: {username} ({user['role']})")
    return token, user["role"]


async def logout(token: str) -> None:
    """Invalidate a session token."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, db.delete_session, token)
    log.debug("[auth] Session invalidated")


# ─── Token extraction ─────────────────────────────────────────────────────────

def _extract_token(request: web.Request) -> Optional[str]:
    """
    Extract a session token from the request.

    Checks in order:
      1. Authorization: Bearer <token> header
      2. session cookie

    Returns the token string or None if not present.
    """
    auth_header = request.headers.get(AUTH_HEADER, "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    token = request.cookies.get(AUTH_COOKIE, "").strip()
    return token or None


# ─── Session resolution ───────────────────────────────────────────────────────

async def resolve_session(request: web.Request) -> Optional[dict]:
    """
    Resolve the session token from a request to a user dict.

    Returns a dict with keys {id, username, role} if the session is
    valid and not expired, otherwise None.

    Result is cached on request["user"] to avoid repeated DB hits
    within the same request lifecycle.
    """
    if "user" in request:
        return request["user"]

    token = _extract_token(request)
    if not token:
        return None

    loop = asyncio.get_event_loop()
    session = await loop.run_in_executor(None, db.get_session, token)
    if session is None:
        return None

    user = await loop.run_in_executor(None, db.get_user_by_id, session["user_id"])
    if user is None:
        return None

    result = {
        "id":       user["id"],
        "username": user["username"],
        "role":     user["role"],
        "token":    token,
    }
    request["user"] = result
    return result


# ─── Access control decorators ───────────────────────────────────────────────

def require_auth(handler):
    """
    Decorator: require any authenticated session (admin or readonly).

    Injects request["user"] = {id, username, role, token}.
    Returns 401 if no valid session, 403 if session exists but user
    record is missing (should not happen in practice).
    """
    @wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        user = await resolve_session(request)
        if user is None:
            return _error("not_authenticated", "Authentication required", 401)
        request["user"] = user
        return await handler(request)
    return wrapper


def require_admin(handler):
    """
    Decorator: require an authenticated session with role 'admin'.

    Returns 401 if not authenticated, 403 if authenticated but not admin.
    """
    @wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        user = await resolve_session(request)
        if user is None:
            return _error("not_authenticated", "Authentication required", 401)
        if user["role"] != "admin":
            return _error("forbidden", "Admin access required", 403)
        request["user"] = user
        return await handler(request)
    return wrapper


# ─── WebSocket auth ───────────────────────────────────────────────────────────

async def ws_resolve_session(request: web.Request) -> Optional[dict]:
    """
    Resolve auth for a WebSocket upgrade request.

    WebSocket clients cannot set the Authorization header in-browser,
    so this checks only the session cookie. API clients using a token
    can pass it as a query parameter: ?token=<token>.

    Returns user dict or None.
    """
    # Check cookie first
    token = request.cookies.get(AUTH_COOKIE, "").strip()

    # Fall back to query param (for non-browser clients / xterm.js)
    if not token:
        token = request.rel_url.query.get("token", "").strip()

    if not token:
        return None

    loop = asyncio.get_event_loop()
    session = await loop.run_in_executor(None, db.get_session, token)
    if session is None:
        return None

    user = await loop.run_in_executor(None, db.get_user_by_id, session["user_id"])
    if user is None:
        return None

    return {
        "id":       user["id"],
        "username": user["username"],
        "role":     user["role"],
        "token":    token,
    }


# ─── First-run bootstrap ──────────────────────────────────────────────────────

_bootstrap_token: Optional[str] = None


def get_bootstrap_token() -> Optional[str]:
    """
    Return the one-time bootstrap token if first-run setup is pending.

    Returns None once at least one user exists.
    """
    return _bootstrap_token


def maybe_generate_bootstrap_token() -> Optional[str]:
    """
    Generate and return a bootstrap token if no users exist yet.

    Prints the token to stdout so the operator can retrieve it from
    container logs. Call once at startup after db.init().

    Returns the token string, or None if users already exist.
    """
    global _bootstrap_token

    if db.user_count() > 0:
        _bootstrap_token = None
        return None

    _bootstrap_token = generate_token()
    print(
        f"\n"
        f"  ┌─────────────────────────────────────────────────────────┐\n"
        f"  │  EchoMuse first-run setup                               │\n"
        f"  │                                                         │\n"
        f"  │  No users found. Visit /setup to create an admin        │\n"
        f"  │  account using this one-time token:                     │\n"
        f"  │                                                         │\n"
        f"  │  {_bootstrap_token[:32]}  │\n"
        f"  │  {_bootstrap_token[32:]}  │\n"
        f"  │                                                         │\n"
        f"  │  This token is not stored and will not appear again.    │\n"
        f"  └─────────────────────────────────────────────────────────┘\n",
        flush=True,
    )
    log.info("[auth] Bootstrap token generated — check stdout")
    return _bootstrap_token


async def create_first_admin(
    bootstrap_token: str,
    username: str,
    password: str,
) -> None:
    """
    Create the first admin user using the bootstrap token.

    Raises AuthError if:
      - the bootstrap token is wrong or setup is already complete
      - a user already exists (setup can only run once)
      - username or password fail basic validation
    """
    global _bootstrap_token

    if _bootstrap_token is None:
        raise AuthError("setup_complete", "Setup has already been completed", 403)

    if not secrets.compare_digest(bootstrap_token, _bootstrap_token):
        raise AuthError("invalid_token", "Invalid setup token", 401)

    if db.user_count() > 0:
        raise AuthError("setup_complete", "A user already exists", 403)

    _validate_credentials(username, password)

    hashed = await hash_password_async(password)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, db.create_user, username, hashed, "admin"
    )

    _bootstrap_token = None  # Consume the token — setup is done
    log.info(f"[auth] First admin created: {username}")


# ─── Credential validation ────────────────────────────────────────────────────

def _validate_credentials(username: str, password: str) -> None:
    """
    Basic validation for username and password.

    Raises AuthError with code 'invalid_input' on failure.
    These are intentionally permissive — the goal is to catch empty
    values and obvious mistakes, not enforce a complex policy.
    """
    if not username or len(username.strip()) < 2:
        raise AuthError("invalid_input", "Username must be at least 2 characters", 400)
    if len(username) > 64:
        raise AuthError("invalid_input", "Username must be 64 characters or fewer", 400)
    if not password or len(password) < 8:
        raise AuthError("invalid_input", "Password must be at least 8 characters", 400)


# ─── AuthError ────────────────────────────────────────────────────────────────

class AuthError(Exception):
    """
    Raised by auth functions to signal a specific, handleable failure.

    Attributes:
        code    — machine-readable error code (see API error shape spec)
        message — human-readable description
        status  — HTTP status code to return
    """
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_response(self) -> web.Response:
        return _error(self.code, self.message, self.status)


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _error(code: str, message: str, status: int) -> web.Response:
    """Consistent error response shape per spec."""
    import json
    return web.Response(
        status=status,
        content_type="application/json",
        body=json.dumps({"error": message, "code": code}),
    )


# A pre-hashed dummy password used to make failed login timing
# indistinguishable from a successful bcrypt check on a wrong password.
# Generated once at module load — never used for actual auth.
_DUMMY_HASH: str = bcrypt.hashpw(
    secrets.token_bytes(32),
    bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
).decode("utf-8")
