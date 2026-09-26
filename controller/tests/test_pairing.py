"""
Pairing: a request from the device, an approval from an admin, and credentials
issued only between the two (Wil, 2026-09-26).

em_pairing is pure; the wiring in em_api and em_controller is checked by ast,
since the suite imports neither.
"""

import ast
from pathlib import Path

import pytest

import em_pairing as P

CONTROLLER = Path(__file__).resolve().parents[1]
DEV = "G090LF1180571VVV"


@pytest.fixture(autouse=True)
def clean():
    P._requests.clear()
    P._approvals.clear()
    yield
    P._requests.clear()
    P._approvals.clear()


# ── state ───────────────────────────────────────────────────────────────────

def test_a_request_is_new_once_then_a_repeat():
    assert P.request(DEV, "plain", now=100) is True
    assert P.request(DEV, "plain", now=105) is False
    assert P.pending_request(DEV, now=106)["first"] == 100


def test_a_request_lapses_when_the_device_stops_asking():
    P.request(DEV, "link", now=100)
    assert P.pending_request(DEV, now=100 + P.REQUEST_TTL_S) is not None
    assert P.pending_request(DEV, now=100 + P.REQUEST_TTL_S + 1) is None
    assert P.request(DEV, "link", now=200) is True     # a fresh ask


def test_an_approval_lapses():
    P.approve(DEV, now=100)
    assert P.approved(DEV, now=100 + P.APPROVAL_TTL_S)
    assert not P.approved(DEV, now=100 + P.APPROVAL_TTL_S + 1)


def test_done_closes_both():
    P.request(DEV, "plain", now=100)
    P.approve(DEV, now=100)
    P.done(DEV)
    assert P.pending_request(DEV, now=100) is None
    assert not P.approved(DEV, now=100)


def test_devices_are_separate():
    P.approve(DEV, now=100)
    assert not P.approved("G090LF11752215LE", now=100)


# ── wiring ──────────────────────────────────────────────────────────────────

def _tree(name):
    return ast.parse((CONTROLLER / name).read_text())


def _fn(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _calls(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", getattr(n.func, "id", None)) == name]


def _names_in_call(call):
    return {getattr(n, "attr", getattr(n, "id", None)) for n in ast.walk(call)}


def test_an_approved_pairing_rotates_before_the_auth_decision():
    fn = _fn(_tree("em_controller.py"), "handle_control")
    clears = [c for c in _calls(fn, "run_in_executor") if "clear_device_token" in _names_in_call(c)]
    auth = _calls(fn, "_link_auth_ok")
    assert clears and auth
    assert clears[0].lineno < auth[0].lineno


def test_a_refused_pairing_dial_is_recorded():
    fn = _fn(_tree("em_controller.py"), "handle_control")
    notes = _calls(fn, "notify_pair_request")
    assert any(isinstance(c.args[1], ast.Constant) and c.args[1].value == "plain" for c in notes)


def test_issuing_rotates_the_token_and_stores_it_only_once_delivered():
    # The push rides the shell plane, which the device dials with its CURRENT
    # token. Storing the new one first refused the push on every device
    # connected over wss (15LE, 2026-09-26).
    fn = _fn(_tree("em_api.py"), "_issue_credentials")
    assert _calls(fn, "token_urlsafe"), "a fresh token, never the stored one"
    assert not [c for c in _calls(fn, "run_in_executor")
                if "ensure_device_token" in _names_in_call(c)]
    stores = [c for c in _calls(fn, "run_in_executor")
              if "set_device_token" in _names_in_call(c)]
    pushes = _calls(fn, "_stream_file_to_device")
    assert stores and pushes
    assert min(c.lineno for c in stores) > max(c.lineno for c in pushes)
    assert _calls(fn, "done")


def test_credentials_are_issued_only_behind_an_approval():
    # _issue_credentials writes the token to whoever holds the connection, so
    # every caller must be one that an admin approval gates.
    callers = set()
    for mod in ("em_api.py", "em_controller.py"):
        tree = _tree(mod)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name != "_issue_credentials":
                if _calls(fn, "_issue_credentials"):
                    callers.add(fn.name)
    assert callers == {"handle_control", "_post_pair"}
    hc = _fn(_tree("em_controller.py"), "handle_control")
    issue = _calls(hc, "_issue_credentials")[0]
    gated = [n for n in ast.walk(hc) if isinstance(n, ast.If)
             and _calls(n.test, "approved") and any(issue is x for x in ast.walk(n))]
    assert gated, "handle_control must issue credentials only under em_pairing.approved"


def test_approving_a_device_opens_the_window():
    assert _calls(_fn(_tree("em_api.py"), "_post_approve"), "approve")


def test_secure_link_is_gone():
    src = (CONTROLLER / "em_api.py").read_text()
    assert "/secure_link" not in src and "_post_secure_link" not in src
    assert "secure_link" not in (CONTROLLER / "static" / "dashboard.jsx").read_text()
