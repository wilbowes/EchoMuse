"""
#159: update_check_interval=0 must mean OFF, not "as fast as possible".

db.get_config returns a STRING, so a stored "0" is truthy and survived the
old `or 3600` fallback into asyncio.sleep(0) — spinning the release poll
loop against api.github.com until it rate-limited, for exactly the user who
set it to stop the controller's only outbound connection (#158). A
non-numeric value raised out of the poll loop and killed the task outright.

Driven against em_releases itself, with the database and GitHub stubbed.
"""

import asyncio
import types

import pytest

import em_releases


@pytest.fixture
def stored(monkeypatch):
    """The system_config the code under test reads; tests fill it in."""
    config: dict = {}
    monkeypatch.setattr(em_releases.db, "get_config",
                        lambda k, d=None: config.get(k, d))
    monkeypatch.setattr(em_releases, "_release_cache", {})
    monkeypatch.setattr(em_releases, "_release_cache_ts", 0.0)
    return config


@pytest.fixture
def fetches(monkeypatch):
    """Every outbound release fetch, recorded instead of made."""
    made = []

    def fake(name, result=None):
        async def f(force=False):
            made.append(name)
            return result
        return f
    monkeypatch.setattr(em_releases, "fetch_firmware", fake("firmware"))
    monkeypatch.setattr(em_releases, "fetch_controller", fake("controller"))
    monkeypatch.setattr(em_releases, "fetch_emos", fake("emos"))
    return made


@pytest.mark.parametrize("value,expected", [
    ("0", 0),          # the case that used to busy-loop
    ("", 3600),        # empty falls back
    ("abc", 3600),     # garbage falls back instead of killing the task
    ("-5", -5),        # negative reads as disabled too
    ("1800", 1800),
    (" 900 ", 900),    # whitespace tolerated
])
def test_interval_parsing_treats_non_positive_as_disabled(stored, value, expected):
    stored["update_check_interval"] = value
    assert em_releases.check_interval() == expected


def _run_poll_loop(monkeypatch, cycles: int) -> list:
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= cycles:
            raise asyncio.CancelledError
    monkeypatch.setattr(em_releases, "asyncio", types.SimpleNamespace(sleep=fake_sleep))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(em_releases.poll_loop())
    return sleeps


def test_poll_loop_disabled_never_fetches_and_parks(monkeypatch, stored, fetches):
    stored["update_check_interval"] = "0"
    sleeps = _run_poll_loop(monkeypatch, cycles=3)   # startup + two parks
    assert fetches == [], "a disabled poll loop must make NO outbound call"
    assert sleeps[0] == 30              # unchanged startup grace
    assert all(s == 60 for s in sleeps[1:]), \
        "disabled must re-read the config periodically, not spin"


def test_poll_loop_enabled_fetches_every_feed_then_sleeps_the_interval(
        monkeypatch, stored, fetches):
    stored["update_check_interval"] = "1800"
    sleeps = _run_poll_loop(monkeypatch, cycles=2)   # startup + first interval
    assert sleeps == [30, 1800]
    assert fetches == ["firmware", "controller", "emos"]


def test_one_feed_failing_does_not_cost_the_others_their_poll(monkeypatch, stored, fetches):
    stored["update_check_interval"] = "1800"

    async def broken(force=False):
        raise RuntimeError("GitHub hiccup")
    monkeypatch.setattr(em_releases, "fetch_firmware", broken)
    _run_poll_loop(monkeypatch, cycles=2)
    assert fetches == ["controller", "emos"]


def test_a_disabled_interval_serves_a_stale_cache_without_fetching(stored, fetches):
    """The dashboard read path awaits a refresh when its cache has aged out,
    which with updates disabled would be an outbound call per visit."""
    stored.update(update_check_interval="0", latest_version="v2.18.1",
                  latest_binary_url="u", last_update_check="0")
    got = asyncio.run(em_releases.cached_firmware())
    assert got["version"] == "v2.18.1"
    assert fetches == []


def test_a_disabled_interval_with_no_cache_answers_none_without_fetching(stored, fetches):
    """
    #159 follow-up: the no-cache branch is the one a FRESH INSTALL takes, and
    that is the install belonging to someone who set the interval to 0 before
    the first poll ever ran. Ungated, it was an outbound call on every
    dashboard visit, permanently: a disabled poll loop never populates the
    cache whose presence would have taken the other branch.
    """
    stored["update_check_interval"] = "0"
    assert asyncio.run(em_releases.cached_firmware()) is None
    assert fetches == []


def test_an_enabled_interval_with_no_cache_fetches(stored, fetches):
    stored["update_check_interval"] = "3600"
    asyncio.run(em_releases.cached_firmware())
    assert fetches == ["firmware"]


def test_a_stale_cache_is_refreshed_when_enabled(stored, fetches):
    stored.update(update_check_interval="60", latest_version="v2.18.0",
                  latest_binary_url="u", last_update_check="0")
    asyncio.run(em_releases.cached_firmware())
    assert fetches == ["firmware"]
