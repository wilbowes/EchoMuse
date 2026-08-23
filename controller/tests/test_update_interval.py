"""
#159: update_check_interval=0 must mean OFF, not "as fast as possible".

db.get_config returns a STRING, so a stored "0" is truthy and survived the
old `or 3600` fallback into asyncio.sleep(0) — spinning the release poll
loop against api.github.com until it rate-limited, for exactly the user who
set it to stop the controller's only outbound connection (#158). A
non-numeric value raised out of the poll loop and killed the task outright.

The functions under test live in em_api.py, which imports aiohttp and the
whole controller stack — deliberately not importable here (see conftest).
Each function's source is extracted and executed in a stub namespace
instead, so what runs is the code that actually ships.
"""

import asyncio
import logging
import types
from pathlib import Path

import pytest

CONTROLLER = Path(__file__).resolve().parents[1]


def _extract(name: str) -> str:
    """The source of one top-level function in em_api.py."""
    src = (CONTROLLER / "em_api.py").read_text()
    start = src.index(f"def {name}")
    if src[max(0, start - 6):start] == "async ":
        start -= 6                       # keep the async keyword
    body = src[start:]
    ends = [i for i in (body.find("\ndef "), body.find("\nasync def "))
            if i != -1]
    return body[:min(ends)]


def _ns(interval_value: str) -> dict:
    """A namespace with db/log stubbed to one stored config value."""
    return {
        "db": types.SimpleNamespace(get_config=(
            lambda k, d=None: {"update_check_interval": interval_value}
            .get(k, d))),
        "log": logging.getLogger("test-update-interval"),
        "asyncio": asyncio,
    }


@pytest.mark.parametrize("stored,expected", [
    ("0", 0),          # the case that used to busy-loop
    ("", 3600),        # empty falls back
    ("abc", 3600),     # garbage falls back instead of killing the task
    ("-5", -5),        # negative reads as disabled too
    ("1800", 1800),
    (" 900 ", 900),    # whitespace tolerated
])
def test_interval_parsing_treats_non_positive_as_disabled(stored, expected):
    ns = _ns(stored)
    exec(_extract("_update_check_interval"), ns)
    assert ns["_update_check_interval"]() == expected


def test_poll_loop_disabled_never_fetches_and_parks():
    fetched, sleeps = [], []

    async def fake_fetch(force=False):
        fetched.append("release")

    async def fake_ctrl(force=True):
        fetched.append("controller")

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:            # startup delay + two park cycles
            raise asyncio.CancelledError

    ns = _ns("0")
    ns.update({
        "_fetch_latest_release": fake_fetch,
        "_fetch_controller_release": fake_ctrl,
        "asyncio": types.SimpleNamespace(sleep=fake_sleep),
    })
    exec(_extract("_update_check_interval"), ns)
    exec(_extract("release_poll_loop"), ns)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ns["release_poll_loop"]())

    assert fetched == [], \
        "a disabled poll loop must make NO outbound call"
    assert sleeps[0] == 30              # unchanged startup grace
    assert all(s == 60 for s in sleeps[1:]), \
        "disabled must re-read the config periodically, not spin"


def test_poll_loop_enabled_fetches_then_sleeps_the_interval():
    fetched, sleeps = [], []

    async def fake_fetch(force=False):
        fetched.append("release")

    async def fake_ctrl(force=True):
        fetched.append("controller")

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:            # startup delay + first interval
            raise asyncio.CancelledError

    ns = _ns("1800")
    ns.update({
        "_fetch_latest_release": fake_fetch,
        "_fetch_controller_release": fake_ctrl,
        "asyncio": types.SimpleNamespace(sleep=fake_sleep),
    })
    exec(_extract("_update_check_interval"), ns)
    exec(_extract("release_poll_loop"), ns)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ns["release_poll_loop"]())

    assert sleeps[0] == 30 and sleeps[1] == 1800
    assert len(fetched) == 2            # both planes polled once per cycle


def test_on_demand_refresh_is_gated_when_disabled():
    """
    The dashboard read path (_get_cached_release) awaits a refresh when its
    cache has aged out — which, with updates disabled, would be an outbound
    GitHub call on every dashboard visit. The fetch must sit behind the
    same `interval > 0` reading as the poll loop.
    """
    fn = _extract("_get_cached_release")
    assert "_update_check_interval()" in fn, \
        "the freshness check must use the shared parser, not raw int()"
    assert "interval > 0" in fn, \
        "a disabled interval must serve the cache without fetching"


def test_the_uncached_path_is_gated_too():
    """
    #159 follow-up: gating only the REFRESH leaves the no-cache branch open.

    _get_cached_release has two exits that can reach GitHub. The first
    guards a controller that has polled successfully at least once. The
    second — "no cache at all — fetch synchronously" — is the branch a
    FRESH INSTALL takes, and that is precisely the install belonging to
    someone who set the interval to 0 before the first poll ever ran.

    Leaving it ungated meant an outbound call on every dashboard visit that
    reads releases, permanently: a disabled poll loop never populates the
    cache whose presence would have taken the other branch.

    Both exits must therefore be behind the same reading. Asserted on the
    shipped source, like the test above, since em_api is deliberately not
    importable here.
    """
    fn = _extract("_get_cached_release")

    # The final fetch is the last statement in the function; everything
    # after the cached-branch `return _release_cache` is the no-cache path.
    tail = fn.rsplit("return _release_cache", 1)[-1]

    # COMMENTS STRIPPED before the ordering check. The prose above the fix
    # names _fetch_latest_release while explaining what "Check now" does, so
    # matching raw source finds it before the code and the ordering assert
    # fails on a correct implementation. A source-shape test has to look at
    # the code, or it is testing the documentation.
    code = "\n".join(l for l in tail.splitlines()
                     if not l.lstrip().startswith("#"))

    assert "_update_check_interval()" in code, \
        "the no-cache path must consult the interval before fetching"
    assert code.index("_update_check_interval()") < code.index("_fetch_latest_release"), \
        "the disabled check must come BEFORE the fetch, not after it"
    assert "return None" in code, \
        "disabled with no cache must answer None, not fetch anyway"
