"""
The device-link auth decision.

Untested by construction until now, because the decision lived inside
em_controller and the suite deliberately does not import that module. What it
cost: deleting a device removed its row, the token is a column on that row, so
`expected` went to None while the device carried on presenting the credential
it still had on disk. That was rejected on all three planes, including the
shell plane the controller would otherwise push a fresh credential over, and
the device retried forever behind a pulsing orange ring with nothing in the
dashboard to explain it (#96).
"""

import em_linkauth as LA

GOOD = "a" * 40
STALE = "b" * 40


def decide(presented=None, expected=None, secure=True, require_tls=False, confirmed=False):
    return LA.decide(presented=presented, expected=expected, confirmed=confirmed,
                     secure=secure, require_tls=require_tls)


# ─── The case that cost a device ──────────────────────────────────────────────

def test_a_token_for_a_device_with_none_on_record_is_admitted():
    """
    A deleted device coming back with its old credential. It must be allowed
    through so it can register as pending, because the shell plane the
    controller would use to fix it is behind this same gate.
    """
    v = decide(presented=STALE, expected=None)
    assert v.ok is True
    assert v.stale_token is True, "the log needs to be able to say why"


def test_a_stale_token_is_no_stricter_than_no_token_at_all():
    """
    The argument for admitting it, as a test rather than a comment: an
    unrecognised token cannot be treated as worse than an absent one while
    anyone can simply omit the header. If these two ever disagree, the
    stricter branch is buying nothing and costing an orphaned device.
    """
    for require_tls in (False, True):
        with_token = decide(presented=STALE, expected=None, require_tls=require_tls)
        without    = decide(presented=None,  expected=None, require_tls=require_tls)
        assert with_token.ok == without.ok, (
            f"require_tls={require_tls}: presenting an unknown token changed "
            f"the outcome versus presenting nothing")


# ─── The cases that must keep rejecting ───────────────────────────────────────

def test_a_mismatched_token_always_rejects():
    """The only case where a credential is actually wrong."""
    v = decide(presented=STALE, expected=GOOD)
    assert v.ok is False
    assert "mismatch" in v.reason


def test_a_mismatched_token_rejects_under_tls_too():
    assert decide(presented=STALE, expected=GOOD, require_tls=True).ok is False


def test_require_tls_refuses_a_plain_connection():
    assert decide(presented=GOOD, expected=GOOD,
                  secure=False, require_tls=True).ok is False


def test_require_tls_refuses_a_device_with_no_stored_token():
    """
    The deleted-device case again, but on an install that has flipped the
    posture. Re-provisioning over USB is the intended path there, and this
    fix must not quietly weaken it.
    """
    assert decide(presented=STALE, expected=None, require_tls=True).ok is False


def test_require_tls_refuses_a_tokenless_connection():
    assert decide(presented=None, expected=GOOD, require_tls=True).ok is False


# ─── The rollout rule that must survive ───────────────────────────────────────

def test_a_stored_token_with_none_presented_is_allowed_by_default():
    """
    The DB row is minted before the credential files reach the device, and the
    push itself rides the shell plane. Rejecting in that window would deadlock
    the rollout it exists to perform.
    """
    v = decide(presented=None, expected=GOOD)
    assert v.ok is True
    assert v.stale_token is False


def test_a_matching_token_is_allowed_everywhere():
    for require_tls in (False, True):
        assert decide(presented=GOOD, expected=GOOD, require_tls=require_tls).ok


def test_an_unknown_device_on_a_default_install_is_allowed():
    """A device that has never been issued a credential, on a default install."""
    v = decide(presented=None, expected=None)
    assert v.ok is True
    assert v.stale_token is False


def test_comparison_is_constant_time():
    """
    The mismatch branch compares secrets, so it must not use ==. Checked on
    the source because timing is not observable from a unit test.
    """
    import inspect
    src = inspect.getsource(LA.decide)
    assert "compare_digest" in src
    assert "presented == expected" not in src


# ─── A token once presented is required (Wil, 2026-09-26) ─────────────────────

def test_a_confirmed_device_without_its_token_is_refused():
    """
    The device id is public: mDNS carries 12 of the serial's 16 characters and
    the rest is a model prefix. Admitting a missing token let anyone on the LAN
    register as any Echo and take its audio.
    """
    for secure in (False, True):
        v = decide(presented=None, expected=GOOD, confirmed=True, secure=secure)
        assert v.ok is False
        assert v.reason == LA.MISSING_CREDENTIAL


def test_an_unconfirmed_device_without_its_token_is_still_admitted():
    # The rollout window: the row is minted before the files reach the device.
    assert decide(presented=None, expected=GOOD, confirmed=False).ok


def test_a_confirmed_device_with_its_token_is_admitted():
    for secure in (False, True):
        assert decide(presented=GOOD, expected=GOOD, confirmed=True, secure=secure).ok


def test_a_confirmed_device_with_the_wrong_token_is_a_mismatch():
    v = decide(presented=STALE, expected=GOOD, confirmed=True)
    assert (v.ok, v.reason) == (False, "token mismatch")


def test_confirmation_means_nothing_without_a_stored_token():
    # A deleted device comes back pending whatever it carries (rule 3).
    assert decide(presented=None, expected=None, confirmed=True).ok
    assert decide(presented=STALE, expected=None, confirmed=True).ok


def test_confirmed_is_required_of_every_caller():
    import pytest
    with pytest.raises(TypeError):
        LA.decide(presented=None, expected=GOOD, secure=True, require_tls=False)


# ─── /data and /shell follow the control connection ──────────────────────────

def follows(control_peer, peer, control_secure=False, secure=False):
    return LA.follows_control(control_peer=control_peer, control_secure=control_secure,
                              peer=peer, secure=secure)


def test_the_same_address_is_admitted():
    assert follows("10.10.1.77", "10.10.1.77") is None
    assert follows("fe80::1", "fe80::1") is None


def test_ipv4_mapped_ipv6_is_the_same_address():
    # A dual-stack listener reports the same peer both ways.
    assert follows("10.10.1.77", "::ffff:10.10.1.77") is None
    assert follows("::ffff:10.10.1.77", "10.10.1.77") is None


def test_an_ipv6_zone_does_not_hide_the_address():
    assert follows("fe80::1%wlan0", "fe80::1") is None
    assert follows("fe80::1%wlan0", "fe80::2") is not None


def test_another_address_is_refused():
    why = follows("10.10.1.77", "10.10.1.200")
    assert why and "10.10.1.200" in why


def test_plain_is_refused_when_control_is_tls():
    assert follows("10.10.1.77", "10.10.1.77", control_secure=True, secure=False)
    assert follows("10.10.1.77", "10.10.1.77", control_secure=True, secure=True) is None
    # The other way round is an upgrade, not an impersonation.
    assert follows("10.10.1.77", "10.10.1.77", control_secure=False, secure=True) is None


def test_an_unreadable_address_admits():
    # Behind a proxy that hides peers there is nothing to compare, and refusing
    # would take every device down.
    for a, b in ((None, "10.0.0.1"), ("10.0.0.1", None), ("", "x"), ("garbage", "10.0.0.1")):
        assert follows(a, b) is None
