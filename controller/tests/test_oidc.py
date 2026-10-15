"""
OpenID Connect sign-in (em_oidc): the decisions, written from the spec's edges.

Everything here is a pure function or runs against a fake issuer on a local
socket. The security content of OIDC sign-in is small and exact — which
groups become which role, and which ID tokens are refused — so it is tested
as such rather than through the dashboard:

  * RFC 7636 appendix B publishes a PKCE verifier and its S256 challenge;
    ours must reproduce it, not merely agree with itself.
  * OIDC Core 3.1.3.7 lists what a client must check on an ID token (iss,
    aud, azp, exp, nonce). Each refusal has its own test, because a check
    that is never exercised is a check nobody notices has gone.
  * Authorisation fails CLOSED: a person in no mapped group gets no role,
    not the lowest one.
"""

import asyncio
import base64
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import em_oidc

ISSUER = "https://auth.example.test/application/o/echomuse/"
CLIENT = "echomuse-client"
CALLBACK = "https://echomuse.example.test/api/auth/oidc/callback"
NOW = 1_800_000_000


def make_config(**over):
    env = {
        "OIDC_ISSUER": ISSUER,
        "OIDC_CLIENT_ID": CLIENT,
        "OIDC_CLIENT_SECRET": "s3cret",
        "OIDC_REDIRECT_URI": CALLBACK,
        "OIDC_ADMIN_GROUPS": "Admins",
        "OIDC_READONLY_GROUPS": "Family",
    }
    env.update(over)
    return em_oidc.config_from_env(env)


def claims(**over):
    base = {
        "iss": ISSUER,
        "aud": CLIENT,
        "sub": "sub-1",
        "exp": NOW + 300,
        "iat": NOW,
        "nonce": "n-1",
        "groups": ["Family"],
    }
    base.update(over)
    return base


def jwt(payload):
    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return ".".join([
        b64(json.dumps({"alg": "RS256"}).encode()),
        b64(json.dumps(payload).encode()),
        b64(b"signature-not-checked-here"),
    ])


# ── Configuration ─────────────────────────────────────────────────────────────

def test_unset_issuer_means_oidc_is_off():
    assert em_oidc.config_from_env({}) is None


def test_partial_configuration_is_an_error_not_a_silent_off():
    # Someone who set an issuer meant to turn this on. Quietly falling back to
    # passwords would look like it worked and leave the login button missing.
    with pytest.raises(em_oidc.ConfigError):
        em_oidc.config_from_env({"OIDC_ISSUER": ISSUER})


def test_no_group_mapping_is_an_error():
    # With nothing mapped, every sign-in would be refused. That is correct,
    # and also never what the person meant.
    with pytest.raises(em_oidc.ConfigError):
        make_config(OIDC_ADMIN_GROUPS="", OIDC_READONLY_GROUPS="")


def test_redirect_uri_must_be_the_callback_route():
    with pytest.raises(em_oidc.ConfigError):
        make_config(OIDC_REDIRECT_URI="https://echomuse.example.test/login")


def test_issuer_must_be_https_unless_loopback():
    with pytest.raises(em_oidc.ConfigError):
        make_config(OIDC_ISSUER="http://auth.example.test/")
    assert make_config(OIDC_ISSUER="http://127.0.0.1:9000/") is not None


def test_group_lists_are_comma_separated_and_trimmed():
    cfg = make_config(OIDC_ADMIN_GROUPS=" Admins , Ops ,,")
    assert cfg.admin_groups == frozenset({"Admins", "Ops"})


def test_password_login_stays_on_unless_switched_off():
    assert make_config().password_login is True
    assert make_config(OIDC_PASSWORD_LOGIN="false").password_login is False


def test_landing_url_is_the_callback_origin_root():
    assert make_config().landing_url == "https://echomuse.example.test/"


# ── Group → role ──────────────────────────────────────────────────────────────

def test_admin_group_gives_admin():
    assert em_oidc.role_for_groups(["Admins"], make_config()) == "admin"


def test_readonly_group_gives_readonly():
    assert em_oidc.role_for_groups(["Family"], make_config()) == "readonly"


def test_admin_wins_when_both_groups_are_held():
    assert em_oidc.role_for_groups(["Family", "Admins"], make_config()) == "admin"


def test_unmapped_group_is_refused_not_downgraded():
    assert em_oidc.role_for_groups(["Friends"], make_config()) is None


def test_no_groups_is_refused():
    assert em_oidc.role_for_groups([], make_config()) is None
    assert em_oidc.role_for_groups(None, make_config()) is None


def test_group_names_are_case_sensitive():
    # "admins" is not "Admins". Authentik group names are case-sensitive and
    # a case-folded match would grant a role to a group nobody mapped.
    assert em_oidc.role_for_groups(["admins"], make_config()) is None


def test_a_single_string_claim_is_one_group_not_a_list_of_characters():
    assert em_oidc.role_for_groups("Admins", make_config()) == "admin"
    assert em_oidc.role_for_groups("A", make_config()) is None


def test_non_string_group_entries_are_ignored():
    assert em_oidc.role_for_groups([None, 7, {"x": 1}], make_config()) is None


# ── PKCE (RFC 7636) ───────────────────────────────────────────────────────────

def test_pkce_challenge_matches_the_rfc_7636_appendix_b_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert (em_oidc.pkce_challenge(verifier)
            == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


def test_new_pkce_verifier_is_within_the_rfc_length_bounds():
    verifier, challenge = em_oidc.new_pkce()
    assert 43 <= len(verifier) <= 128
    assert challenge == em_oidc.pkce_challenge(verifier)


def test_two_flows_never_share_state_or_nonce():
    assert em_oidc.random_token() != em_oidc.random_token()


# ── Authorization URL ─────────────────────────────────────────────────────────

def test_authorization_url_carries_code_flow_pkce_state_and_nonce():
    from urllib.parse import urlsplit, parse_qs
    url = em_oidc.authorization_url(
        "https://auth.example.test/authorize/", make_config(),
        state="st", nonce="no", challenge="ch")
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert parts.path == "/authorize/"
    assert q["response_type"] == "code"
    assert q["client_id"] == CLIENT
    assert q["redirect_uri"] == CALLBACK
    assert q["state"] == "st" and q["nonce"] == "no"
    assert q["code_challenge"] == "ch"
    assert q["code_challenge_method"] == "S256"
    assert "openid" in q["scope"].split()


# ── ID token claims (OIDC Core 3.1.3.7) ───────────────────────────────────────

def check(c, **over):
    return em_oidc.validate_claims(c, make_config(), nonce="n-1", now=NOW, **over)


def test_a_good_token_yields_an_identity():
    ident = check(claims(preferred_username="anthony"))
    assert ident.subject == "sub-1"
    assert ident.issuer == ISSUER
    assert ident.username == "anthony"
    assert ident.groups == ["Family"]


@pytest.mark.parametrize("field, value", [
    ("iss", "https://evil.example.test/"),
    ("aud", "someone-elses-client"),
    ("aud", [CLIENT, "second-audience"]),   # multi-audience without azp
    ("exp", NOW - 61),                       # past the 60s clock-skew allowance
    ("nonce", "replayed"),
    ("sub", ""),
    ("sub", None),
])
def test_refused_tokens(field, value):
    with pytest.raises(em_oidc.OidcError):
        check(claims(**{field: value}))


def test_missing_nonce_is_refused():
    c = claims()
    del c["nonce"]
    with pytest.raises(em_oidc.OidcError):
        check(c)


def test_audience_list_is_accepted_when_azp_names_us():
    check(claims(aud=[CLIENT, "other"], azp=CLIENT))


def test_audience_list_with_foreign_azp_is_refused():
    with pytest.raises(em_oidc.OidcError):
        check(claims(aud=[CLIENT, "other"], azp="other"))


def test_slightly_expired_token_is_tolerated_within_skew():
    check(claims(exp=NOW - 30))


def test_groups_claim_name_is_configurable():
    cfg = make_config(OIDC_GROUPS_CLAIM="roles")
    c = claims(roles=["Admins"])
    del c["groups"]
    ident = em_oidc.validate_claims(c, cfg, nonce="n-1", now=NOW)
    assert ident.groups == ["Admins"]


def test_username_falls_back_through_email_then_subject():
    assert check(claims(email="wil@example.test")).username == "wil"
    assert check(claims()).username.startswith("oidc-")


def test_username_is_trimmed_and_bounded():
    long_name = "x" * 500
    ident = check(claims(preferred_username=f"  {long_name}  "))
    assert ident.username == "x" * 64


# ── One-shot store ────────────────────────────────────────────────────────────

def test_store_pop_is_single_use():
    store = em_oidc.OneShotStore(ttl_s=60, max_entries=8, clock=lambda: 0)
    store.put("k", {"v": 1})
    assert store.pop("k") == {"v": 1}
    assert store.pop("k") is None


def test_store_entry_expires():
    now = [0]
    store = em_oidc.OneShotStore(ttl_s=60, max_entries=8, clock=lambda: now[0])
    store.put("k", "v")
    now[0] = 61
    assert store.pop("k") is None


def test_store_is_bounded_so_an_unauthenticated_route_cannot_fill_memory():
    store = em_oidc.OneShotStore(ttl_s=60, max_entries=3, clock=lambda: 0)
    for i in range(10):
        store.put(f"k{i}", i)
    assert len(store) == 3
    assert store.pop("k9") == 9      # newest survive
    assert store.pop("k0") is None   # oldest evicted


# ── Talking to an issuer ──────────────────────────────────────────────────────

class FakeIssuer:
    """An OpenID provider reduced to what the controller calls."""

    def __init__(self, id_token_claims=None, discovery_issuer=None,
                 token_status=200, token_endpoint=None,
                 authorization_endpoint=None, redirect_token_to=None):
        self.id_token_claims = id_token_claims
        self.discovery_issuer = discovery_issuer
        self.token_status = token_status
        self.token_endpoint = token_endpoint
        self.authorization_endpoint = authorization_endpoint
        self.redirect_token_to = redirect_token_to
        self.landing_requests = []
        self.token_requests = []
        self.base = ""

    async def discovery(self, request):
        return web.json_response({
            "issuer": self.discovery_issuer or self.base,
            "authorization_endpoint":
                self.authorization_endpoint or f"{self.base}authorize/",
            "token_endpoint": self.token_endpoint or f"{self.base}token/",
        })

    async def landing(self, request):
        self.landing_requests.append(dict(await request.post()))
        return web.json_response({"id_token": jwt(self.id_token_claims)})

    async def token(self, request):
        self.token_requests.append(dict(await request.post()))
        if self.redirect_token_to:
            # 307 keeps the method and body, which is what carries the secret.
            raise web.HTTPTemporaryRedirect(self.redirect_token_to)
        if self.token_status != 200:
            return web.json_response({"error": "invalid_grant"},
                                     status=self.token_status)
        return web.json_response({
            "access_token": "at", "token_type": "Bearer",
            "id_token": jwt(self.id_token_claims),
        })


async def with_issuer(issuer, body):
    app = web.Application()
    app.router.add_get("/.well-known/openid-configuration", issuer.discovery)
    app.router.add_post("/token/", issuer.token)
    app.router.add_post("/elsewhere/", issuer.landing)
    server = TestServer(app)
    await server.start_server()
    try:
        issuer.base = str(server.make_url("/"))
        cfg = make_config(OIDC_ISSUER=issuer.base)
        return await body(cfg)
    finally:
        await server.close()


def test_discovery_reads_the_endpoints():
    async def body(cfg):
        return await em_oidc.discover(cfg)
    endpoints = asyncio.run(with_issuer(FakeIssuer(), body))
    assert endpoints.authorization_endpoint.endswith("/authorize/")
    assert endpoints.token_endpoint.endswith("/token/")


def test_discovery_refuses_a_document_naming_a_different_issuer():
    # RFC 8414 3.3 / OIDC Discovery 4.3: the issuer in the document must be
    # the one it was fetched for, or a hijacked path can point us anywhere.
    async def body(cfg):
        return await em_oidc.discover(cfg)
    with pytest.raises(em_oidc.OidcError):
        asyncio.run(with_issuer(
            FakeIssuer(discovery_issuer="https://elsewhere.example.test/"), body))


def test_code_exchange_posts_the_verifier_and_returns_claims():
    issuer = FakeIssuer()

    async def body(cfg):
        issuer.id_token_claims = claims(iss=issuer.base)
        endpoints = await em_oidc.discover(cfg)
        return await em_oidc.exchange_code(cfg, endpoints, code="c-1",
                                           verifier="v-1")
    got = asyncio.run(with_issuer(issuer, body))
    sent = issuer.token_requests[0]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "c-1"
    assert sent["code_verifier"] == "v-1"
    assert sent["redirect_uri"] == CALLBACK
    assert got["sub"] == "sub-1"


def test_a_refused_code_is_an_oidc_error():
    issuer = FakeIssuer(token_status=400)

    async def body(cfg):
        endpoints = await em_oidc.discover(cfg)
        return await em_oidc.exchange_code(cfg, endpoints, code="bad",
                                           verifier="v")
    with pytest.raises(em_oidc.OidcError):
        asyncio.run(with_issuer(issuer, body))


# ── What the discovery document may point us at ───────────────────────────────
#
# The client secret and the authorization code go to whatever
# token_endpoint the discovery document names, and the ID token is trusted
# without a signature check BECAUSE it arrives over TLS from that endpoint.
# So the endpoint has to be one that actually gives that guarantee.

@pytest.mark.parametrize("field", ["token_endpoint", "authorization_endpoint"])
def test_discovery_refuses_a_plain_http_endpoint(field):
    async def body(cfg):
        return await em_oidc.discover(cfg)
    issuer = FakeIssuer(**{field: "http://auth.example.test/endpoint/"})
    with pytest.raises(em_oidc.OidcError):
        asyncio.run(with_issuer(issuer, body))


def test_discovery_accepts_an_https_endpoint_on_another_host():
    # Providers such as Google legitimately split hosts, so same-origin is
    # not required; only that the channel is encrypted.
    async def body(cfg):
        return await em_oidc.discover(cfg)
    issuer = FakeIssuer(token_endpoint="https://tokens.example.test/token")
    endpoints = asyncio.run(with_issuer(issuer, body))
    assert endpoints.token_endpoint == "https://tokens.example.test/token"


def test_token_request_does_not_follow_a_redirect_with_the_secret():
    issuer = FakeIssuer()

    async def body(cfg):
        issuer.redirect_token_to = f"{issuer.base}elsewhere/"
        issuer.id_token_claims = claims(iss=issuer.base)
        endpoints = await em_oidc.discover(cfg)
        return await em_oidc.exchange_code(cfg, endpoints, code="c",
                                           verifier="v")
    with pytest.raises(em_oidc.OidcError):
        asyncio.run(with_issuer(issuer, body))
    assert issuer.landing_requests == [], \
        "the redirect target received the client secret"


# ── Flow cookie name ──────────────────────────────────────────────────────────

def test_https_flow_cookie_uses_the_host_prefix():
    # __Host- makes the browser refuse a cookie set with a Domain attribute,
    # which is how a sibling subdomain would plant one.
    name, path, secure = em_oidc.flow_cookie(make_config())
    assert name == "__Host-em_oidc_state"
    assert path == "/"
    assert secure is True


def test_loopback_http_flow_cookie_keeps_the_narrow_path():
    # __Host- requires Secure, which a plain-http loopback test setup cannot
    # have, so this is the only deployment that gets the unprefixed name.
    cfg = make_config(OIDC_ISSUER="http://127.0.0.1:9000/",
                      OIDC_REDIRECT_URI="http://127.0.0.1:8768/api/auth/oidc/callback")
    name, path, secure = em_oidc.flow_cookie(cfg)
    assert name == "em_oidc_state"
    assert path == "/api/auth/oidc"
    assert secure is False
