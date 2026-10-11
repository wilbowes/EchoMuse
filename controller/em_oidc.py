"""
em_oidc.py — OpenID Connect sign-in for the dashboard.

What it decides, and nothing else: which ID tokens are believed, and which
role a person's groups earn. The routes that use it live in em_api, the
session and account handling in em_auth; this module has no database and no
request objects, so the whole security surface is testable without either.

The flow is the authorization-code flow with PKCE (RFC 7636), as a
confidential client. The ID token is fetched by the controller directly from
the issuer's token endpoint over TLS, which is the case OIDC Core 3.1.3.7
allows a client to skip checking the signature for: the TLS channel to the
issuer is what authenticates it. Every claim check in that section is still
made — iss, aud, azp, exp and nonce — and each has its own test.

Authorisation fails CLOSED. A person in none of the mapped groups gets no
role at all, not the lowest one, so removing someone from a group in the
identity provider is what removes their access.

Configuration is environment-only, like the rest of the controller's
deployment settings, and a client secret has no business in the database:

    OIDC_ISSUER           issuer URL (https, or http on loopback)  — enables OIDC
    OIDC_CLIENT_ID        client id registered with the provider
    OIDC_CLIENT_SECRET    client secret
    OIDC_REDIRECT_URI     https://<dashboard host>/api/auth/oidc/callback
    OIDC_ADMIN_GROUPS     comma-separated group names that become `admin`
    OIDC_READONLY_GROUPS  comma-separated group names that become `readonly`
    OIDC_GROUPS_CLAIM     claim holding the groups (default `groups`)
    OIDC_SCOPES           default `openid profile email groups`
    OIDC_PASSWORD_LOGIN   `false` turns the password form off (default on)
    OIDC_SESSION_HOURS    session lifetime for OIDC sign-ins (default 12)
    OIDC_BUTTON_LABEL     text on the sign-in button (default `Sign in with SSO`)
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlencode, urlsplit

import aiohttp

# Where the callback route is mounted. The redirect URI must end with this,
# so the dashboard's landing page can be derived from it rather than guessed.
CALLBACK_PATH = "api/auth/oidc/callback"

DEFAULT_SCOPES = "openid profile email groups"
DEFAULT_GROUPS_CLAIM = "groups"
DEFAULT_SESSION_HOURS = 12
DEFAULT_BUTTON_LABEL = "Sign in with SSO"

# Clock skew tolerated on `exp`. OIDC Core leaves the figure to the client.
CLOCK_SKEW_S = 60

# How long a started sign-in, or a finished one waiting to be collected by the
# browser, stays valid.
FLOW_TTL_S = 600
EXCHANGE_TTL_S = 60

# Both stores are filled by unauthenticated requests, so both are bounded.
MAX_PENDING = 256

HTTP_TIMEOUT_S = 10
MAX_USERNAME_LEN = 64

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(ValueError):
    """The OIDC environment is set but not usable."""


class OidcError(Exception):
    """A sign-in step failed. The message is for the log, never the browser."""


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    admin_groups: frozenset
    readonly_groups: frozenset
    groups_claim: str
    scopes: str
    password_login: bool
    session_hours: float
    button_label: str

    @property
    def landing_url(self) -> str:
        """The dashboard's own origin root, where the browser is sent back."""
        return self.redirect_uri[: -len(CALLBACK_PATH)]


def _groups(raw: str) -> frozenset:
    return frozenset(g.strip() for g in raw.split(",") if g.strip())


def _issuer_is_acceptable(issuer: str) -> bool:
    parts = urlsplit(issuer)
    if parts.scheme == "https" and parts.netloc:
        return True
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


def config_from_env(env: Mapping[str, str]) -> Optional[OidcConfig]:
    """
    Read the OIDC settings. None when OIDC is not configured at all.

    Raises ConfigError for a half-configured install, because the person who
    set an issuer meant to turn this on, and quietly serving only the
    password form would look like success while the button never appeared.
    """
    def get(key: str, default: str = "") -> str:
        return (env.get(key) or default).strip()

    issuer = get("OIDC_ISSUER")
    if not issuer:
        return None

    missing = [k for k in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET",
                           "OIDC_REDIRECT_URI") if not get(k)]
    if missing:
        raise ConfigError(f"OIDC_ISSUER is set but {', '.join(missing)} is not")

    if not _issuer_is_acceptable(issuer):
        raise ConfigError("OIDC_ISSUER must be https (http only on loopback)")

    redirect_uri = get("OIDC_REDIRECT_URI")
    if not redirect_uri.endswith(CALLBACK_PATH):
        raise ConfigError(f"OIDC_REDIRECT_URI must end with /{CALLBACK_PATH}")

    admin = _groups(get("OIDC_ADMIN_GROUPS"))
    readonly = _groups(get("OIDC_READONLY_GROUPS"))
    if not admin and not readonly:
        raise ConfigError(
            "OIDC_ADMIN_GROUPS and OIDC_READONLY_GROUPS are both empty, "
            "so nobody could ever sign in")

    try:
        hours = float(get("OIDC_SESSION_HOURS", str(DEFAULT_SESSION_HOURS)))
    except ValueError:
        raise ConfigError("OIDC_SESSION_HOURS must be a number") from None
    if hours <= 0:
        raise ConfigError("OIDC_SESSION_HOURS must be positive")

    return OidcConfig(
        issuer=issuer,
        client_id=get("OIDC_CLIENT_ID"),
        client_secret=get("OIDC_CLIENT_SECRET"),
        redirect_uri=redirect_uri,
        admin_groups=admin,
        readonly_groups=readonly,
        groups_claim=get("OIDC_GROUPS_CLAIM", DEFAULT_GROUPS_CLAIM),
        scopes=get("OIDC_SCOPES", DEFAULT_SCOPES),
        password_login=get("OIDC_PASSWORD_LOGIN", "true").lower()
        not in ("false", "0", "no", "off"),
        session_hours=hours,
        button_label=get("OIDC_BUTTON_LABEL", DEFAULT_BUTTON_LABEL),
    )


# ─── Flow cookie ──────────────────────────────────────────────────────────────

FLOW_COOKIE = "em_oidc_state"
FLOW_COOKIE_PATH = "/api/auth/oidc"


def flow_cookie(cfg: OidcConfig) -> tuple[str, str, bool]:
    """
    (name, path, secure) for the cookie that binds a callback to the browser
    that started the sign-in.

    Over https it carries the `__Host-` prefix, which makes the browser
    refuse a cookie set with a Domain attribute. Without it a page on any
    sibling subdomain can plant a cookie of the same name for the whole
    parent domain and pair a victim's callback with the attacker's own
    state. The prefix requires Secure and Path=/, so plain-http loopback —
    which cannot be Secure — keeps the unprefixed name and the narrow path.
    """
    if cfg.redirect_uri.startswith("https://"):
        return f"__Host-{FLOW_COOKIE}", "/", True
    return FLOW_COOKIE, FLOW_COOKIE_PATH, False


# ─── Authorisation ────────────────────────────────────────────────────────────

def role_for_groups(groups: Any, cfg: OidcConfig) -> Optional[str]:
    """
    The role these groups earn, or None for no access.

    Admin wins when both are held. Matching is exact and case-sensitive: a
    case-folded match would grant a role to a group nobody mapped. A bare
    string is one group — iterating it would turn "Admins" into six letters.
    """
    if isinstance(groups, str):
        groups = [groups]
    if not isinstance(groups, (list, tuple, set, frozenset)):
        return None
    held = {g for g in groups if isinstance(g, str)}
    if held & cfg.admin_groups:
        return "admin"
    if held & cfg.readonly_groups:
        return "readonly"
    return None


# ─── PKCE and flow secrets ────────────────────────────────────────────────────

def random_token(nbytes: int = 32) -> str:
    """An unguessable URL-safe string, for state, nonce and exchange codes."""
    return secrets.token_urlsafe(nbytes)


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 4.2: BASE64URL(SHA256(verifier)), unpadded."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_pkce() -> tuple[str, str]:
    """A fresh (verifier, challenge). 32 random bytes encode to 43 characters,
    the RFC's minimum, drawn from its unreserved alphabet."""
    verifier = random_token(32)
    return verifier, pkce_challenge(verifier)


def authorization_url(authorization_endpoint: str, cfg: OidcConfig, *,
                      state: str, nonce: str, challenge: str) -> str:
    query = urlencode({
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{query}"


# ─── ID token claims ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    username: str
    groups: list


def _username_from(claims: Mapping[str, Any], subject: str) -> str:
    candidates = [
        claims.get("preferred_username"),
        (claims.get("email") or "").split("@")[0],
        claims.get("name"),
    ]
    for value in candidates:
        if isinstance(value, str):
            cleaned = "".join(c for c in value.strip() if c.isprintable())
            if cleaned:
                return cleaned[:MAX_USERNAME_LEN]
    return f"oidc-{subject[:12]}"


def validate_claims(claims: Mapping[str, Any], cfg: OidcConfig, *,
                    nonce: str, now: float) -> Identity:
    """OIDC Core 3.1.3.7: refuse a token that is not for us, not from the
    issuer, expired, or not the answer to the request we made."""
    if claims.get("iss") != cfg.issuer:
        raise OidcError("issuer mismatch")

    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud
    if not isinstance(audiences, list) or cfg.client_id not in audiences:
        raise OidcError("token is not for this client")
    if len(audiences) > 1 and claims.get("azp") != cfg.client_id:
        raise OidcError("multi-audience token without our azp")

    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp + CLOCK_SKEW_S < now:
        raise OidcError("token expired")

    got_nonce = claims.get("nonce")
    if not isinstance(got_nonce, str) or not secrets.compare_digest(
            got_nonce.encode(), nonce.encode()):
        raise OidcError("nonce mismatch")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise OidcError("token has no subject")

    raw_groups = claims.get(cfg.groups_claim)
    if isinstance(raw_groups, str):
        raw_groups = [raw_groups]
    groups = [g for g in raw_groups if isinstance(g, str)] \
        if isinstance(raw_groups, list) else []

    return Identity(issuer=cfg.issuer, subject=subject,
                    username=_username_from(claims, subject), groups=groups)


# ─── One-shot, bounded, expiring store ────────────────────────────────────────

class OneShotStore:
    """
    In-memory map whose entries can be taken exactly once and expire.

    Holds a started sign-in (state → verifier and nonce) and a finished one
    waiting for the browser to collect it. Memory-only on purpose: a
    controller restart abandons a sign-in in flight, and the person clicks
    the button again. Bounded because the route that fills it is public.
    """

    def __init__(self, ttl_s: float, max_entries: int = MAX_PENDING,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_s
        self._max = max_entries
        self._clock = clock
        self._items: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def put(self, key: str, value: Any) -> None:
        self._drop_expired()
        self._items[key] = (self._clock() + self._ttl, value)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def pop(self, key: str) -> Any:
        entry = self._items.pop(key, None)
        if entry is None:
            return None
        expires, value = entry
        return value if expires > self._clock() else None

    def _drop_expired(self) -> None:
        now = self._clock()
        for key in [k for k, (exp, _) in self._items.items() if exp <= now]:
            del self._items[key]


# ─── Talking to the issuer ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Endpoints:
    authorization_endpoint: str
    token_endpoint: str


def _timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)


async def discover(cfg: OidcConfig) -> Endpoints:
    """Fetch the provider's discovery document and refuse one that names a
    different issuer than the one it was fetched for (OIDC Discovery 4.3)."""
    url = cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        async with aiohttp.ClientSession(timeout=_timeout()) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise OidcError(f"discovery returned HTTP {resp.status}")
                doc = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise OidcError(f"discovery failed: {exc}") from exc

    if not isinstance(doc, dict) or doc.get("issuer") != cfg.issuer:
        raise OidcError("discovery document names a different issuer")
    auth_ep = doc.get("authorization_endpoint")
    token_ep = doc.get("token_endpoint")
    if not isinstance(auth_ep, str) or not isinstance(token_ep, str):
        raise OidcError("discovery document lacks the endpoints we need")
    # The secret and the code go to token_endpoint, and the ID token is
    # believed without a signature check because it arrives over TLS from
    # there. An endpoint that is not encrypted voids both. Same-origin is
    # not required: some providers split hosts legitimately.
    for name, url in (("authorization", auth_ep), ("token", token_ep)):
        if not _issuer_is_acceptable(url):
            raise OidcError(f"discovery {name}_endpoint is not https")
    return Endpoints(auth_ep, token_ep)


def _jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise OidcError("id_token is not a JWT")
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcError("id_token payload is unreadable") from exc
    if not isinstance(payload, dict):
        raise OidcError("id_token payload is not an object")
    return payload


async def exchange_code(cfg: OidcConfig, endpoints: Endpoints, *,
                        code: str, verifier: str) -> dict:
    """Trade an authorization code for the ID token's claims. The token comes
    straight from the issuer over TLS — see the module docstring."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "code_verifier": verifier,
    }
    try:
        async with aiohttp.ClientSession(timeout=_timeout()) as session:
            # No redirects: a 307/308 would re-send the client secret and
            # the code, body included, to wherever it points.
            async with session.post(endpoints.token_endpoint, data=form,
                                    allow_redirects=False) as resp:
                if resp.status != 200:
                    raise OidcError(f"token endpoint returned HTTP {resp.status}")
                body = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise OidcError(f"token request failed: {exc}") from exc

    id_token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(id_token, str):
        raise OidcError("token response carried no id_token")
    return _jwt_payload(id_token)
