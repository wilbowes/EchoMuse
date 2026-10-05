"""
LOG_LEVELS: what a hand-typed per-logger spec does, and what it does not.

The decisions these pin are the ones a person gets wrong by accident. The
DEBUG env var's own bug is the shape to remember — every non-empty string is
truthy in Python, so `os.environ.get("DEBUG")` alone put every add-on install
at DEBUG with the toggle showing off — and anything that tests a LOG_LEVELS
value for truthiness is that bug again.

Global logging state is real and shared, so every test that calls `apply`
runs under `levels_restored`, which puts every logger back exactly as it was
and drops anything a test created.
"""

import logging
import pathlib
import re

import pytest

import em_loglevel

CONTROLLER = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def levels_restored():
    """
    Undo every level change and logger creation a test caused.

    The manager's table also holds PlaceHolder entries for parents of loggers
    something else created, and those have no level to restore.
    """
    table = logging.Logger.manager.loggerDict
    before = {name: logger.level for name, logger in table.items()
              if isinstance(logger, logging.Logger)}
    yield
    for name in [name for name in table if name not in before]:
        del table[name]
    for name, level in before.items():
        table[name].setLevel(level)


def _make(name: str) -> str:
    """A live logger, so `apply` has something real to reach."""
    logging.getLogger(name)
    return name


# ── The parse ────────────────────────────────────────────────────────────────

def test_empty_spec_is_not_an_error(levels_restored):
    """Unset, empty and whitespace-only are the same thing: no pairs."""
    for spec in ("", "   ", ",", " , "):
        result = em_loglevel.parse(spec)
        assert result.levels == {}, spec
        assert result.problems == (), spec


def test_one_pair(levels_restored):
    assert em_loglevel.parse("echomuse.esphome=DEBUG").levels == \
        {"echomuse.esphome": logging.DEBUG}


def test_several_pairs(levels_restored):
    parsed = em_loglevel.parse("echomuse.esphome=DEBUG,aiohttp.access=INFO")
    assert parsed.levels == {"echomuse.esphome": logging.DEBUG,
                             "aiohttp.access": logging.INFO}


def test_whitespace_around_both_sides(levels_restored):
    """A .env line has spaces after the commas whether or not you wrote them."""
    assert em_loglevel.parse(
        " echomuse.db = WARNING , aiohttp.access=ERROR ").levels == \
        {"echomuse.db": logging.WARNING, "aiohttp.access": logging.ERROR}


def test_level_names_are_case_insensitive(levels_restored):
    assert em_loglevel.parse("echomuse.db=warning").levels == \
        {"echomuse.db": logging.WARNING}


def test_unknown_level_is_skipped_not_fatal(levels_restored):
    """
    One bad level must cost one setting, not the whole string: the other
    pair still applies and the reason comes back for a warning.
    """
    parsed = em_loglevel.parse("echomuse.db=LOUD,echomuse.pki=INFO")
    assert parsed.levels == {"echomuse.pki": logging.INFO}
    assert len(parsed.problems) == 1
    assert "LOUD" in parsed.problems[0]


def test_pair_with_no_equals_is_reported(levels_restored):
    parsed = em_loglevel.parse("echomuse.db")
    assert parsed.levels == {}
    assert "name=LEVEL" in parsed.problems[0]


def test_a_dotless_name_that_logs_is_honoured(levels_restored):
    """
    `echomuse` and `aiohttp` are real loggers, so setting the root of a
    hierarchy is the obvious thing to want to do — and refusing it left the
    documented example of the feature unusable.
    """
    name = _make("dotless")                       # no dot, and it does log
    result = em_loglevel.apply(f"{name}=DEBUG")
    assert result.problems == ()
    assert logging.getLogger(name).level == logging.DEBUG


def test_every_documented_example_applies(levels_restored):
    """
    The examples in `.env.example`, the add-on option's help text and this
    module's own docstring are the three places somebody copies from, and the
    first version of this refused two of the three. Push each one through
    `apply()` rather than restating them, so a doc that drifts fails here.
    """
    # The third-party names are documented because they are the ones people
    # reach for, and both are live in the controller. The suite imports neither
    # package, so a logger is only in the manager's table once something has
    # asked for it — materialise them the way importing them would.
    for live in ("echomuse", "echomuse.esphome", "aiohttp", "aiohttp.access"):
        _make(live)

    documented = {
        "echomuse": logging.DEBUG,           # the dotless root
        "echomuse.esphome": logging.DEBUG,
        "aiohttp.access": logging.INFO,
        "aiohttp": logging.DEBUG,            # the other dotless one
    }
    spec = ",".join(f"{n}={logging.getLevelName(lv)}" for n, lv in documented.items())
    result = em_loglevel.apply(spec)
    assert result.problems == (), result.problems
    for name, want in documented.items():
        assert logging.getLogger(name).level == want, name


# ── Applying it ──────────────────────────────────────────────────────────────

def test_apply_sets_the_named_logger(levels_restored):
    name = _make("echomuse.applytest")
    result = em_loglevel.apply(f"{name}=DEBUG")
    assert result.levels == {name: logging.DEBUG}
    assert logging.getLogger(name).level == logging.DEBUG


def test_unknown_logger_name_is_skipped_and_does_not_exist(levels_restored):
    """
    A name nothing logs to would otherwise be a setting that is accepted,
    displayed and silently ignored — so it is dropped, named in `problems`,
    and no stray logger is left behind.
    """
    result = em_loglevel.apply("nosucharea.quiet=DEBUG")
    assert result.levels == {}
    assert "nosucharea.quiet" in result.problems[0]
    assert "nosucharea.quiet" not in logging.Logger.manager.loggerDict


def test_a_name_under_an_existing_logger_is_honoured(levels_restored):
    """
    Matched on the nearest existing ancestor, not exactly: a logger is only
    in the table once something asked for it, so `echomuse.player` must still
    work on a controller that has not imported em_player yet.
    """
    _make("echomuse.applyroot")
    assert em_loglevel.apply("echomuse.applyroot.later=DEBUG").levels == \
        {"echomuse.applyroot.later": logging.DEBUG}


def test_a_bad_pair_does_not_stop_the_good_ones(levels_restored):
    name = _make("echomuse.applytest")
    result = em_loglevel.apply(f"broken,{name}=WARNING")
    assert result.levels == {name: logging.WARNING}


def test_an_override_can_be_less_verbose_than_the_baseline(levels_restored):
    """
    The decision: applied as written, both ways. With the global level at
    INFO, `echomuse.db=WARNING` DOES quiet the database logger — otherwise
    one string means different things depending on DEBUG, and there is no
    way to quiet a single firehose without turning everything on first.
    """
    baseline = logging.getLogger()
    original = baseline.level
    try:
        baseline.setLevel(logging.INFO)
        name = _make("echomuse.applytest")
        em_loglevel.apply(f"{name}=WARNING")
        assert baseline.level == logging.INFO, "the global level is unchanged"
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    finally:
        baseline.setLevel(original)


def test_an_override_can_be_more_verbose_than_the_baseline(levels_restored):
    """The other direction, and the one DEBUG is the shorthand for."""
    baseline = logging.getLogger()
    original = baseline.level
    try:
        baseline.setLevel(logging.INFO)
        name = _make("echomuse.applytest")
        em_loglevel.apply(f"{name}=DEBUG")
        assert logging.getLogger(name).getEffectiveLevel() == logging.DEBUG
    finally:
        baseline.setLevel(original)


def test_a_parent_override_reaches_every_child(levels_restored):
    """
    `echomuse=DEBUG` must reach `echomuse.esphome` and `echomuse.player`
    without anything here visiting them — that is logging's own effective
    level, not a hierarchy of our own. If this ever needs a loop to pass, we
    have reimplemented inheritance badly.
    """
    root = _make("echomuse.applyroot")
    child = logging.getLogger("echomuse.applyroot.esphome")
    assert child.level == logging.NOTSET, "the child must stay unset"
    em_loglevel.apply(f"{root}=DEBUG")
    assert child.getEffectiveLevel() == logging.DEBUG


# ── The two ways the hierarchy goes wrong again ─────────────────────────────

def test_the_controller_does_not_truthiness_test_the_spec():
    """
    The DEBUG trap in the shape it actually appears: every non-empty string
    is truthy in Python and em_start.py renders a false add-on option as the
    string "0", so a bare `if os.environ.get("LOG_LEVELS")` guard would turn
    an untouched add-on option into whatever "0" parses to. The value is
    parsed as text by em_loglevel and never asked whether it is true.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    assert not re.search(r'if\s+os\.environ\.get\(\s*"LOG_LEVELS"', src), \
        "LOG_LEVELS is being read as a bare truthiness test"


def test_every_logger_the_controller_defines_is_reachable():
    """
    `em_player` logged as the bare name "player" for the life of the tree
    (#378), so no `echomuse` level and no per-logger override could reach it,
    and it looked right only because it inherited the root level by accident.
    A logger defined outside the hierarchy cannot be given a level by name,
    which is the failure this guards — verified by reintroducing it.
    """
    # Third-party loggers the controller deliberately quiets. These are not
    # ours and have no echomuse parent to inherit from; they are named here
    # because they are, deliberately, set individually.
    third_party = {"websockets.server", "aiohttp.access"}

    offenders = []
    for path in sorted(CONTROLLER.glob("*.py")):
        for line in path.read_text().splitlines():
            found = re.search(r'getLogger\(\s*(?:(__name__)|"([^"]*)")', line)
            if found is None:
                continue
            name = found.group(2)
            if name is None:            # getLogger(__name__) is the module
                offenders.append(f"{path.name}: getLogger(__name__)")
            elif not (name == "echomuse" or name.startswith("echomuse.")
                       or name in third_party):
                offenders.append(f"{path.name}: getLogger({name!r})")

    assert not offenders, (
        "logger names outside the echomuse hierarchy — a per-area level "
        f"cannot reach these: {offenders}")