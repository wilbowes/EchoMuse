"""
Tests for em_ingressauth — trusting Home Assistant's authentication.

This is a login path, so the tests are written from the direction that
matters: every way of NOT being a genuine ingress request must return None.
A false positive here is an unauthenticated admin session on a dashboard
that proxies a root shell.
"""

import em_ingressauth as ia

GATEWAY = ia.INGRESS_GATEWAY_IP


def _decide(**kw):
    base = dict(ingress_only=True, remote=GATEWAY, user_id="abc123")
    base.update(kw)
    return ia.decide(**base)


# ── The happy path ────────────────────────────────────────────────────────────

def test_genuine_ingress_request_is_authenticated():
    got = _decide(username="wil", display_name="Wil Bowes")
    assert got == ia.IngressIdentity("abc123", "wil", "Wil Bowes")


def test_display_name_alone_is_enough():
    # Supervisor sets each name header only if the HA user record has one.
    assert _decide(username=None, display_name="Wil Bowes").username == "Wil Bowes"


def test_username_alone_is_enough():
    assert _decide(username="wil", display_name=None).display_name == "wil"


def test_no_names_at_all_still_authenticates():
    # The id is the only header Supervisor always sets. Requiring a name
    # would lock out a user whose HA record has neither.
    got = _decide(username=None, display_name=None)
    assert got is not None
    assert got.user_id == "abc123"
    assert got.username  # something usable, derived from the id


# ── Refusals: the whole point ─────────────────────────────────────────────────

def test_standalone_container_never_trusts_the_header():
    # THE bug this module exists to prevent: on the standalone container the
    # header is attacker-supplied, so honouring it is a login bypass.
    assert ia.decide(
        ingress_only=False, remote=GATEWAY,
        user_id="abc123", username="wil",
    ) is None


def test_standalone_refuses_even_from_the_gateway_address():
    # Belt and braces — INGRESS_ONLY is the deployment fact, and it alone
    # decides whether the header means anything.
    assert ia.decide(
        ingress_only=False, remote=GATEWAY, user_id="abc123") is None


def test_lan_request_to_the_addon_is_refused():
    assert _decide(remote="192.168.1.50") is None


def test_loopback_is_not_the_gateway():
    # host_network puts us in the host's netns, so localhost is reachable
    # and is not Supervisor.
    assert _decide(remote="127.0.0.1") is None


def test_missing_remote_is_refused():
    assert _decide(remote=None) is None


def test_near_miss_gateway_address_is_refused():
    # Exact match only — no prefix or subnet logic anywhere.
    assert _decide(remote="172.30.32.20") is None
    assert _decide(remote="172.30.32.2 ") is None
    assert _decide(remote="1172.30.32.2") is None


def test_absent_user_id_falls_back_to_password_login():
    # An ingress session with no user attached. Being told nothing is not
    # the same as being told it is nobody.
    assert _decide(user_id=None) is None


def test_blank_user_id_is_refused():
    assert _decide(user_id="") is None
    assert _decide(user_id="   ") is None


# ── Shape ─────────────────────────────────────────────────────────────────────

def test_the_gateway_address_is_not_configurable_from_a_header():
    # Documents intent: the constant is the only source of the address.
    assert GATEWAY == "172.30.32.2"


def test_whitespace_is_stripped_from_identity_fields():
    got = _decide(user_id="  abc123  ", username="  wil  ")
    assert got.user_id == "abc123"
    assert got.username == "wil"


# ── Role assignment ───────────────────────────────────────────────────────────

def test_first_home_assistant_user_becomes_admin():
    # Mirrors the standalone container, where whoever holds the bootstrap
    # token becomes the owner. This is what removes the bootstrap step.
    assert ia.role_for(existing_ha_admins=0, configured_default=None) == "admin"


def test_first_user_is_admin_regardless_of_the_configured_default():
    assert ia.role_for(
        existing_ha_admins=0, configured_default="readonly") == "admin"


def test_later_users_are_readonly():
    # HA's ingress view does not gate on admin (requires_auth=False;
    # panel_admin only hides the sidebar entry), so reaching the dashboard is
    # not evidence of being trusted with a root shell to every device.
    assert ia.role_for(
        existing_ha_admins=1, configured_default=None) == "readonly"


def test_a_local_admin_does_not_deny_admin_to_the_first_ha_user():
    """
    #235, and the whole reason this counts HA admins rather than user rows.

    Local password accounts cannot be signed into under ingress at all, so a
    local admin is an admin nobody can use. Counting it made every Home
    Assistant user read-only forever, reachable by the ordinary documented
    act of copying a container's /data across when moving to the add-on.

    The caller passes ha_admin_count(), so a database full of local users
    still presents zero here.
    """
    assert ia.role_for(existing_ha_admins=0, configured_default=None) == "admin"
    assert ia.role_for(
        existing_ha_admins=0, configured_default="readonly") == "admin"


def test_a_controller_with_no_reachable_admin_can_recover():
    """
    If every HA admin is demoted or deleted, the next new HA user becomes
    admin. That is the recovery path working, not a hole: a controller nobody
    can administer is the broken state this exists to escape, and it grants
    exactly what a fresh install already grants the first person through.
    """
    assert ia.role_for(existing_ha_admins=0, configured_default=None) == "admin"


def test_an_operator_can_opt_into_auto_admin():
    assert ia.role_for(
        existing_ha_admins=3, configured_default="admin") == "admin"


def test_an_unrecognised_configured_role_falls_back_to_readonly():
    # A typo in a config row must never be the thing that grants admin.
    for bad in ("Admin", "administrator", "root", "", "   ", "None"):
        assert ia.role_for(
            existing_ha_admins=1, configured_default=bad) == "readonly"


def test_role_for_only_ever_returns_a_known_role():
    for n in (0, 1, 99):
        for d in (None, "admin", "readonly", "nonsense"):
            assert ia.role_for(
                existing_ha_admins=n, configured_default=d) in ia.VALID_ROLES
