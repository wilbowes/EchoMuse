"""
Controller test suite, shared setup.

The suite imports `em_api`, `em_controller` and `em_esphome` since 2026-10-03
(#739); before that it covered pure-logic modules only. It needs:

    pip install pytest pytest-cov numpy scipy pyyaml aiohttp websockets bcrypt zeroconf protobuf

openwakeword is not installed. A test that needs em_controller calls
`_oww_stub.install()`; see that file for why it is per test.

Nothing here needs a live socket, a device or real audio. The coverage
baseline is in `.coveragerc`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))