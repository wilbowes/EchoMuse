"""
The login page wires single sign-on the way the routes expect.

Source-level guards on static/index.html (it has no build step and no DOM
under pytest); the behaviour that can be run is in oidc_login.test.mjs.
What these pin is the ORDER and the ROUTES, because getting either wrong
still renders a plausible page:

  * the one-shot code is cleared from the address bar BEFORE it is
    exchanged, so it is not in history and a reload does not replay it;
  * the exchange is a POST, so the session token is never in a URL;
  * the button's label comes from the server and is set as text, never HTML;
  * the sign-in button is a relative link, so it works under Home Assistant
    ingress where the dashboard is not at the site root.
"""

from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
PAGE = (CONTROLLER / "static" / "index.html").read_text()


def _fn(name: str) -> str:
    """Source of one function in the page, up to the next top-level one."""
    start = PAGE.index(f"function {name}(")
    nxt = PAGE.find("\n    async function ", start + 1)
    nxt2 = PAGE.find("\n    function ", start + 1)
    ends = [e for e in (nxt, nxt2) if e != -1]
    return PAGE[start:min(ends)] if ends else PAGE[start:]


def test_button_is_a_relative_link_to_the_login_route():
    assert 'href="api/auth/oidc/login"' in PAGE
    assert "'/api/auth/oidc" not in PAGE and '"/api/auth/oidc' not in PAGE, \
        "absolute paths bypass the ingress base"


def test_page_asks_the_server_whether_sso_is_on():
    assert "api/auth/oidc/config" in _fn("applySso")


def test_applied_after_the_login_form_is_shown_so_it_cannot_remove_the_way_in():
    init = _fn("init")
    assert init.index("show('form-login')") < init.index("await applySso()")


def test_code_is_cleared_from_the_url_before_it_is_exchanged():
    body = _fn("finishOidcSignIn")
    assert body.index("history.replaceState") < body.index("api/auth/oidc/exchange")


def test_exchange_is_a_post_and_enters_the_dashboard_as_oidc():
    body = _fn("finishOidcSignIn")
    assert "post('api/auth/oidc/exchange'" in body
    assert "'oidc'" in body, "the session must be recorded as obtained via oidc"


def test_label_is_set_as_text_not_markup():
    body = _fn("applySso")
    assert "sso-btn" in body
    assert "textContent" in body and "innerHTML" not in body


def test_password_form_hides_when_the_server_turns_it_off():
    assert "password_login" in _fn("applySso")
    assert "show('')" in _fn("applySso")


def test_an_unreachable_provider_does_not_take_the_password_form_away():
    # applySso swallows its own failure: a thrown error would skip the error
    # message and leave the page half-drawn.
    body = _fn("applySso")
    assert "try {" in body and "catch" in body
