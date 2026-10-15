"""
The OpenID Connect routes, driven through the real app with a fake provider.

The flow under test, end to end:

    GET  /api/auth/oidc/login      -> 302 to the provider, flow cookie set
    GET  /api/auth/oidc/callback   -> 302 to the dashboard with #oidc=<code>
    POST /api/auth/oidc/exchange   -> {token, role}

What each hop is there to prevent:

  * the cookie binds the callback to the browser that started the flow, so a
    link an attacker sends cannot sign a victim in as the attacker;
  * the fragment (not a query string) carries the one-shot code, so it never
    reaches a log or a Referer header, and the code is swapped for a session
    by a POST, so the session token is never in a URL at all;
  * every failure redirects with a FIXED error code — nothing the provider or
    the client sent is reflected into the page.
"""

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer

import em_api
import em_auth
import em_db as db
import em_oidc

ISSUER = "https://auth.example.test/application/o/echomuse/"
CALLBACK = "https://echomuse.example.test/api/auth/oidc/callback"
LANDING = "https://echomuse.example.test/"
COOKIE = "__Host-em_oidc_state"
AUTHORIZE = "https://auth.example.test/authorize/"


def make_cfg(**env):
    base = {
        "OIDC_ISSUER": ISSUER,
        "OIDC_CLIENT_ID": "echomuse",
        "OIDC_CLIENT_SECRET": "s3cret",
        "OIDC_REDIRECT_URI": CALLBACK,
        "OIDC_ADMIN_GROUPS": "Admins",
        "OIDC_READONLY_GROUPS": "Family",
        "OIDC_BUTTON_LABEL": "Sign in with Authentik",
    }
    base.update(env)
    return em_oidc.config_from_env(base)


class Provider:
    """Stands in for em_oidc.discover / exchange_code."""

    def __init__(self):
        self.claims = None          # set per test; nonce is filled in on use
        self.exchanges = []

    async def discover(self, cfg):
        return em_oidc.Endpoints(AUTHORIZE, "https://auth.example.test/token/")

    async def exchange_code(self, cfg, endpoints, *, code, verifier):
        self.exchanges.append({"code": code, "verifier": verifier})
        return dict(self.claims)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db.init(str(tmp_path / "test.db"))
    provider = Provider()
    monkeypatch.setattr(em_oidc, "discover", provider.discover)
    monkeypatch.setattr(em_oidc, "exchange_code", provider.exchange_code)
    monkeypatch.setattr(em_api, "_oidc_cfg", make_cfg())
    monkeypatch.setattr(em_api, "_oidc_discovery_cache", None)
    monkeypatch.setattr(em_api, "_oidc_flows",
                        em_oidc.OneShotStore(em_oidc.FLOW_TTL_S))
    monkeypatch.setattr(em_api, "_oidc_exchanges",
                        em_oidc.OneShotStore(em_oidc.EXCHANGE_TTL_S))
    yield provider
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def run(coro_fn):
    """Run `coro_fn(client)` against the real app."""
    async def main():
        app = await em_api.create_app()
        async with TestClient(TestServer(app)) as client:
            return await coro_fn(client)
    return asyncio.run(main())


async def start_flow(client, provider, **claim_over):
    """Do the /login hop and return (state, cookie_value, nonce)."""
    resp = await client.get("/api/auth/oidc/login", allow_redirects=False)
    assert resp.status == 302
    query = parse_qs(urlsplit(resp.headers["Location"]).query)
    state, nonce = query["state"][0], query["nonce"][0]
    provider.claims = {
        "iss": ISSUER, "aud": "echomuse", "sub": "sub-1",
        "exp": 4_000_000_000, "nonce": nonce,
        "preferred_username": "anthony", "groups": ["Family"],
    }
    provider.claims.update(claim_over)
    return state, resp.cookies[COOKIE].value, nonce


def callback(client, state, cookie, **extra):
    params = {"code": "auth-code", "state": state, **extra}
    return client.get("/api/auth/oidc/callback", params=params,
                      cookies={COOKIE: cookie} if cookie else {},
                      allow_redirects=False)


def error_of(resp):
    assert resp.status == 302
    loc = urlsplit(resp.headers["Location"])
    assert f"{loc.scheme}://{loc.netloc}/" == LANDING
    return parse_qs(loc.query).get("oidc_error", [None])[0]


def code_of(resp):
    assert resp.status == 302
    loc = urlsplit(resp.headers["Location"])
    assert f"{loc.scheme}://{loc.netloc}/" == LANDING
    assert loc.query == "", "nothing sensitive may ride in the query string"
    return parse_qs(loc.fragment)["oidc"][0]


# ── Config ────────────────────────────────────────────────────────────────────

def test_config_reports_disabled_when_oidc_is_not_configured(env, monkeypatch):
    monkeypatch.setattr(em_api, "_oidc_cfg", None)

    async def body(client):
        resp = await client.get("/api/auth/oidc/config")
        return resp.status, await resp.json()
    status, data = run(body)

    assert status == 200
    assert data == {"enabled": False, "password_login": True}


def test_config_exposes_only_what_the_login_page_needs(env):
    async def body(client):
        return await (await client.get("/api/auth/oidc/config")).json()
    data = run(body)

    assert data == {"enabled": True, "label": "Sign in with Authentik",
                    "password_login": True}


def test_login_route_is_absent_when_oidc_is_off(env, monkeypatch):
    monkeypatch.setattr(em_api, "_oidc_cfg", None)

    async def body(client):
        return (await client.get("/api/auth/oidc/login",
                                 allow_redirects=False)).status
    assert run(body) == 404


# ── Login hop ─────────────────────────────────────────────────────────────────

def test_login_redirects_to_the_provider_and_sets_a_flow_cookie(env):
    async def body(client):
        resp = await client.get("/api/auth/oidc/login", allow_redirects=False)
        return resp.status, resp.headers["Location"], resp.cookies[COOKIE]
    status, location, cookie = run(body)

    assert status == 302
    assert location.startswith(AUTHORIZE + "?")
    q = parse_qs(urlsplit(location).query)
    assert cookie.value == q["state"][0]
    assert cookie["httponly"]
    assert cookie["path"] == "/", "__Host- cookies must be scoped to /"
    assert not cookie["domain"], "__Host- cookies must not carry a Domain"
    assert cookie["samesite"].lower() == "lax"
    assert cookie["secure"], "the redirect URI is https, so the cookie must be Secure"


def test_login_survives_the_provider_being_unreachable(env, monkeypatch):
    async def down(cfg):
        raise em_oidc.OidcError("connection refused")
    monkeypatch.setattr(em_oidc, "discover", down)

    async def body(client):
        resp = await client.get("/api/auth/oidc/login", allow_redirects=False)
        return resp.status, await resp.text()
    status, text = run(body)

    assert status == 503
    assert "connection refused" not in text


# ── The whole flow ────────────────────────────────────────────────────────────

def test_full_flow_ends_in_a_working_session_with_the_mapped_role(env):
    async def body(client):
        state, cookie, _ = await start_flow(client, env)
        code = code_of(await callback(client, state, cookie))
        ex = await client.post("/api/auth/oidc/exchange", json={"code": code})
        data = await ex.json()
        me = await client.get("/api/auth/me", headers={
            "Authorization": f"Bearer {data['token']}"})
        return ex.status, data, await me.json()
    status, data, me = run(body)

    assert status == 200
    assert data["role"] == "readonly"
    assert data["via"] == "oidc"
    assert me["username"] == "anthony"
    assert me["role"] == "readonly"


def test_the_verifier_sent_to_the_provider_matches_the_challenge(env):
    async def body(client):
        resp = await client.get("/api/auth/oidc/login", allow_redirects=False)
        q = parse_qs(urlsplit(resp.headers["Location"]).query)
        env.claims = {"iss": ISSUER, "aud": "echomuse", "sub": "s",
                      "exp": 4_000_000_000, "nonce": q["nonce"][0],
                      "groups": ["Family"]}
        await callback(client, q["state"][0], resp.cookies[COOKIE].value)
        return q["code_challenge"][0]
    challenge = run(body)

    assert em_oidc.pkce_challenge(env.exchanges[0]["verifier"]) == challenge


def test_admin_group_produces_an_admin_session(env):
    async def body(client):
        state, cookie, _ = await start_flow(client, env, groups=["Admins"])
        code = code_of(await callback(client, state, cookie))
        return await (await client.post("/api/auth/oidc/exchange",
                                        json={"code": code})).json()
    assert run(body)["role"] == "admin"


# ── Refusals ──────────────────────────────────────────────────────────────────

def test_callback_without_the_flow_cookie_is_refused_before_any_exchange(env):
    async def body(client):
        state, _, _ = await start_flow(client, env)
        return error_of(await callback(client, state, cookie=None))
    assert run(body) == "expired"
    assert env.exchanges == []


def test_callback_with_someone_elses_cookie_is_refused(env):
    async def body(client):
        state, _, _ = await start_flow(client, env)
        return error_of(await callback(client, state, cookie="attacker-state"))
    assert run(body) == "expired"
    assert env.exchanges == []


def test_a_cookie_planted_under_the_old_name_is_not_honoured(env):
    # A sibling subdomain can set `em_oidc_state` for the whole parent
    # domain; only the __Host- name is immune to that, so the callback must
    # not fall back to the unprefixed one.
    async def body(client):
        resp = await client.get("/api/auth/oidc/login", allow_redirects=False)
        state = parse_qs(urlsplit(resp.headers["Location"]).query)["state"][0]
        env.claims = {"iss": ISSUER, "aud": "echomuse", "sub": "s",
                      "exp": 4_000_000_000, "nonce": "x", "groups": ["Family"]}
        planted = await client.get(
            "/api/auth/oidc/callback",
            params={"code": "c", "state": state},
            cookies={"em_oidc_state": state}, allow_redirects=False)
        return error_of(planted)
    assert run(body) == "expired"
    assert env.exchanges == []


def test_a_state_can_only_be_used_once(env):
    async def body(client):
        state, cookie, _ = await start_flow(client, env)
        code_of(await callback(client, state, cookie))
        return error_of(await callback(client, state, cookie))
    assert run(body) == "expired"


def test_provider_error_is_a_fixed_code_not_reflected_text(env):
    async def body(client):
        state, cookie, _ = await start_flow(client, env)
        resp = await callback(client, state, cookie,
                              error="access_denied",
                              error_description="<script>alert(1)</script>")
        return error_of(resp), resp.headers["Location"]
    code, location = run(body)

    assert code == "denied"
    assert "script" not in location


def test_a_person_in_no_mapped_group_gets_no_code_and_no_account(env):
    async def body(client):
        state, cookie, _ = await start_flow(client, env, groups=["Friends"])
        return error_of(await callback(client, state, cookie))
    assert run(body) == "no_access"
    assert db.get_all_users() == []


def test_a_token_with_the_wrong_nonce_is_refused(env):
    async def body(client):
        state, cookie, _ = await start_flow(client, env, nonce="not-ours")
        return error_of(await callback(client, state, cookie))
    assert run(body) == "failed"
    assert db.get_all_users() == []


def test_a_provider_failure_during_exchange_is_a_fixed_failed_code(env, monkeypatch):
    async def broken(cfg, endpoints, *, code, verifier):
        raise em_oidc.OidcError("HTTP 500 with secret detail")
    monkeypatch.setattr(em_oidc, "exchange_code", broken)

    async def body(client):
        state, cookie, _ = await start_flow(client, env)
        resp = await callback(client, state, cookie)
        return error_of(resp), resp.headers["Location"]
    code, location = run(body)

    assert code == "failed"
    assert "secret" not in location


def test_exchange_code_is_single_use(env):
    async def body(client):
        state, cookie, _ = await start_flow(client, env)
        code = code_of(await callback(client, state, cookie))
        first = await client.post("/api/auth/oidc/exchange", json={"code": code})
        second = await client.post("/api/auth/oidc/exchange", json={"code": code})
        return first.status, second.status
    assert run(body) == (200, 401)


def test_unknown_exchange_code_is_unauthorised(env):
    async def body(client):
        resp = await client.post("/api/auth/oidc/exchange",
                                 json={"code": "made-up"})
        return resp.status
    assert run(body) == 401


# ── Password login and role editing ───────────────────────────────────────────

def seed_local_admin():
    db.create_user("break-glass", em_auth.hash_password("correct horse"), "admin")


def test_password_login_still_works_by_default(env):
    seed_local_admin()

    async def body(client):
        resp = await client.post("/api/auth/login", json={
            "username": "break-glass", "password": "correct horse"})
        return resp.status
    assert run(body) == 200


def test_password_login_can_be_switched_off_and_says_why(env, monkeypatch):
    seed_local_admin()
    monkeypatch.setattr(em_api, "_oidc_cfg", make_cfg(OIDC_PASSWORD_LOGIN="false"))

    async def body(client):
        resp = await client.post("/api/auth/login", json={
            "username": "break-glass", "password": "correct horse"})
        return resp.status, (await resp.json())["code"]
    assert run(body) == (403, "password_login_disabled")


def test_an_oidc_accounts_role_cannot_be_edited_by_hand(env):
    # It would be overwritten at that person's next sign-in, so letting the
    # edit succeed would only be a lie that expires.
    seed_local_admin()
    oidc_id = db.create_oidc_user(ISSUER, "sub-1", "anthony", "readonly")

    async def body(client):
        login = await (await client.post("/api/auth/login", json={
            "username": "break-glass", "password": "correct horse"})).json()
        resp = await client.patch(f"/api/users/{oidc_id}",
                                  json={"role": "admin"},
                                  headers={"Authorization":
                                           f"Bearer {login['token']}"})
        users = await (await client.get("/api/users", headers={
            "Authorization": f"Bearer {login['token']}"})).json()
        return resp.status, (await resp.json())["code"], users
    status, code, users = run(body)

    assert (status, code) == (409, "oidc_linked")
    assert db.get_user_by_id(oidc_id)["role"] == "readonly"
    assert {u["username"]: u["oidc_linked"] for u in users} == {
        "break-glass": False, "anthony": True}
