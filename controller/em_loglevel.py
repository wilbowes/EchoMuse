"""
em_loglevel.py — the LOG_LEVELS string, parsed and applied.

Split out of `em_controller` for the reason `em_linkauth` is: the test suite
cannot import em_controller, so a decision living there is a decision nobody
can test. What is here is a string parse and a `setLevel`, which needs none
of the aiohttp/zeroconf/openwakeword stack.

LOG_LEVELS is a comma-separated list of `name=LEVEL` pairs:

    LOG_LEVELS="echomuse.esphome=DEBUG,aiohttp.access=INFO"

Each pair sets ONE logger's level, applied on top of the global level
DEBUG chose, so a pair is read as written in BOTH directions: with the
baseline at INFO, `echomuse.db=WARNING` does quiet the database logger.

The alternative — an override may only ever make a logger MORE verbose —
was considered and rejected. It makes one string mean different things
depending on DEBUG, and it leaves no way to quiet a single firehose without
turning the whole controller to DEBUG first, which is a fight nobody should
have while chasing an intermittent fault. The cost is that a stale override
survives `DEBUG=1`; the answer is that the override is the specific
instruction and DEBUG is the broad one, and both are stated in the startup
line this emits.

THE HIERARCHY IS PYTHON'S, NOT OURS
----------------------------------
`echomuse=DEBUG` reaches `echomuse.esphome` and `echomuse.player` because
logging resolves a logger's effective level from the nearest ancestor that
has one (`Logger.getEffectiveLevel`). Nothing is walked here, no child is
visited, no child needs a level set. That is the whole reason a logger
outside the hierarchy is a bug rather than a style choice: `em_player`
logged as the bare name `player`, so no `echomuse` level could ever have
reached the loudest logger in the tree, and it looked correct only because
an unset DEBUG left it inheriting the root level by accident.

A DOTLESS NAME IS HONOURED, because `echomuse` and `aiohttp` are real
loggers and setting the root of a hierarchy is the obvious thing to want
to do. The mistake this would otherwise hide — `player=DEBUG` for a
logger that ought to be `echomuse.player` — is caught below instead, by
the name naming no logger anything logs to.

A NAME NOTHING LOGS TO IS SKIPPED, AND SAYS SO
----------------------------------------------
Setting a level on a logger that has never been created would silently do
nothing, which is the failure this project keeps paying for: a setting that
is accepted, stored, displayed and ignored. An unknown name is therefore
skipped with a warning naming it. It is matched against the nearest
EXISTING ancestor rather than exactly, so a name can be written before the
module that logs under it exists and will apply once it does.

A BAD PAIR WARNS, IT DOES NOT STOP THE CONTROLLER
-------------------------------------------------
An unknown level, a pair with no `=`, a name nothing logs to: each is a
warning and the rest of the string still applies. This is `em_start.py`'s choice
for one stray option key, and for the same reason — an add-on that refuses
to boot over a typo in a DIAGNOSTICS setting has turned a support problem
into an outage, and the person who has to solve it cannot get to a log.
The alternative (fail fast) is right for a setting that changes what the
controller DOES, and wrong for one that only changes what it says.

THE DEBUG TRAP, RESTATED
------------------------
`os.environ.get("DEBUG")` alone turned debug logging ON for `DEBUG=0`,
because every non-empty string is truthy in Python and `em_start.py`
renders a false add-on option as exactly the string `"0"` — so an untouched
"Debug logging" toggle shipped every add-on install at DEBUG. Nothing here
tests a LOG_LEVELS value for truthiness: unset, empty and whitespace are
all the same thing, meaning no pairs and no level changed.
"""

from __future__ import annotations

import logging
from typing import NamedTuple


class Overrides(NamedTuple):
    """
    What a LOG_LEVELS string asked for, and what could not be honoured.

    `levels` is what was APPLIED — an unknown logger name is dropped from
    it and named in `problems`, so a caller that logs this shows what the
    controller is actually doing rather than what was requested.
    """
    levels: dict[str, int]
    problems: tuple[str, ...]


def parse(spec: str) -> Overrides:
    """
    A LOG_LEVELS string to {logger name: level}, without touching logging.

    Level names are case-insensitive and either side of an `=` may carry
    whitespace; anything unrecognisable is collected in `problems` and the
    remaining pairs are still returned, so one bad pair costs one setting
    rather than the whole string.
    """
    levels: dict[str, int] = {}
    problems: list[str] = []

    for pair in spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, sep, level = pair.partition("=")
        name, level = name.strip(), level.strip()
        if not sep or not name or not level:
            problems.append(f"{pair!r} is not name=LEVEL")
            continue
        value = logging.getLevelNamesMapping().get(level.upper())
        if value is None:
            problems.append(f"{level!r} is not a log level in {pair!r}")
            continue
        levels[name] = value

    return Overrides(levels, tuple(problems))


def apply(spec: str) -> Overrides:
    """
    Apply a LOG_LEVELS string to the live loggers, and report what happened.

    Called after the global level is set, because these win over it. A name
    is honoured when it or its nearest ancestor already exists as a logger;
    anything else is dropped with a warning, so a typo cannot read as a
    setting that took effect.
    """
    parsed = parse(spec)
    applied: dict[str, int] = {}
    problems = list(parsed.problems)

    for name, level in parsed.levels.items():
        if not _reaches_a_logger(name):
            problems.append(f"{name!r} is not a logger anything logs to")
            continue
        logging.getLogger(name).setLevel(level)
        applied[name] = level

    return Overrides(applied, tuple(problems))


def _reaches_a_logger(name: str) -> bool:
    """
    Whether `name` is a live logger or sits under one.

    An ancestor match rather than an exact one, because a logger is only in
    the manager's table once something has asked for it: a name written for
    a module this controller does not import yet is a forward reference, not
    a mistake, and it starts applying when that module appears.
    """
    parts = name.split(".")
    while parts:
        if ".".join(parts) in logging.Logger.manager.loggerDict:
            return True
        parts.pop()
    return False