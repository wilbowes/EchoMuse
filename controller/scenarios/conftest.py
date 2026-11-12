"""
Scenario tests: the real controller process, a fake Echo, and Home Assistant
played by aioesphomeapi. See harness.py.

Kept apart from tests/ and run as their own pytest invocation (CI's
"Scenario tests" step): aioesphomeapi registers the same ESPHome protobuf
messages as our vendored esphome/ package, and protobuf refuses the second
registration in one process, which broke every unit test that imports
em_esphome. This process imports only aioesphomeapi; the controller runs in
its own.

Skipped where aioesphomeapi is not installed; CI installs it.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("aioesphomeapi")
pytest.importorskip("websockets")

import harness  # noqa: E402


@pytest.fixture
def scenario(tmp_path):
    """Run an async scenario against a fresh controller.

    The test is `async def body(ctl)`; the fixture returns a runner so each
    test owns its event loop and the controller dies with the test, whatever
    happens. On failure the controller's log tail is attached.
    """
    def run(body, timeout: float = 60.0, **env):
        ctl = harness.Controller(tmp_path, **env)

        async def main():
            await ctl.start()
            try:
                await asyncio.wait_for(body(ctl), timeout)
            except BaseException as e:
                tail = "\n".join(ctl.log().splitlines()[-80:])
                raise AssertionError(f"{e}\n--- controller log tail ---\n{tail}") from e
            finally:
                ctl.stop()
        asyncio.run(main())
    return run
