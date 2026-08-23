"""
#197: role management needs a screen, not an API call.

Under the add-on the first Home Assistant user to open the dashboard
becomes admin; everyone after is read-only — and read-only is
load-bearing (recordings and transcripts are admin-only, and the
dashboard proxies a root shell to every device). The recovery path for
"the wrong person clicked first" was asking them to make an
authenticated API call on your behalf.

GET /api/users and PATCH /api/users/{id} always existed and were tested;
what was missing was any UI calling them. These guards pin that the
Settings panel now carries that screen.
"""

from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _settings_src() -> str:
    src = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    start = src.index("function SettingsPanel")
    return src[start:start + 20_000]


def test_users_tab_exists_and_is_admin_only():
    tabs = _settings_src()[_settings_src().index("const TABS"):]
    tabs = tabs[:tabs.index(";")]
    assert "'users'" in tabs, "the Users tab must exist in Settings"
    # TABS already gates on isAdmin for account/support; users rides along.
    assert "isAdmin ?" in tabs


def test_users_screen_calls_both_existing_endpoints():
    src = _settings_src()
    assert "'/api/users'" in src, "the screen must list accounts"
    assert "API.patch(`/api/users/${id}`" in src or \
           'API.patch("/api/users/"' in src, \
        "the promote/demote control must call PATCH /api/users/{id}"


def test_server_reason_reaches_the_user():
    """
    The server refuses demoting the last admin and explains ha_linked
    accounts; a generic 'failed' would send users hunting a bug that
    isn't there. The error field must be surfaced verbatim.
    """
    src = _settings_src()
    fn = src[src.index("async function setUserRole"):]
    fn = fn[:fn.index("async function loadUsers") if "async function loadUsers" in fn else 800]
    assert "e.error ||" in fn, "the PATCH refusal reason must be shown"
