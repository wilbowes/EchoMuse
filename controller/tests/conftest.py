"""
Controller test suite — shared setup.

WHAT THE SUITE COVERS, and why it changed on 2026-10-03.

Until then the suite covered "the pure-logic modules only" — nothing that needs
aiohttp, zeroconf, bcrypt, websockets, protobuf or openwakeword. That was not a
preference for test purity; it was a statement about what would install. The
five lightweight ones were already pinned in requirements.txt and already in the
published image, and installing them costs seconds:

    pip install aiohttp websockets bcrypt zeroconf protobuf

That one change makes em_api.py (2,463 statements) and em_esphome.py (1,202)
importable, so they can be DRIVEN rather than read as text. Before it both were
covered only by source-scraping guards — 218 of the suite's test functions (17%
of the total) asserted on substrings of em_api / em_controller / em_esphome and
executed not one line of them. em_api measured 0.0%.

MEASURED COVERAGE is in .coveragerc, which records the baseline this replaced:
10,980 statements, 7,019 missed, 36.1% (commit 48e94ef).

OPENWAKEWORD is the exception and is deliberately NOT installed: it drags in
onnxruntime and scikit-learn, which is minutes of install for nothing this suite
needs. em_controller imports `from openwakeword.model import Model as OWWModel`
at module level, so reaching em_controller at all means stubbing it — see
`_oww_stub.install`, which is opt-in per test rather than done here.

That it is opt-in rather than unconditional is not tidiness. Four existing
tests call `pytest.importorskip("openwakeword")` to skip when the real package
is absent, and a conftest-level stub would satisfy that check: they would stop
skipping and then fail on the missing `MODELS` table. A stub that changes what
other tests can see is not a stub, it is a global.

The suite still deliberately avoids anything needing a live socket, a device, or
real audio. "Reachable by import" is not "testable", and the honest statement of
what is untested is still the coverage number rather than a passing suite.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))