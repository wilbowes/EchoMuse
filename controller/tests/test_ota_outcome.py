"""
What an unconfirmed firmware update is reported as.

The verdict read only the stored firmware version, which changes only when a
device registers. A device that died on its update and never came back still
holds its old version, so it was reported "auto-rolled back to <old>", which
reads as a healthy Echo. Found driving an OTA at a fake Echo that did not
redial, 2026-10-15.
"""

import ast

import pytest

import _sources
import em_ota


@pytest.mark.parametrize("connected,running,previous,expected", [
    (False, "v2.18.1", "v2.18.1", "not_back"),     # THE bug: dead, row unchanged
    (False, None,      "v2.18.1", "not_back"),
    (True,  "v2.18.1", "v2.18.1", "rolled_back"),  # start_server.sh gave up
    (True,  "v2.17.0", "v2.18.1", "timeout"),      # back on something else
])
def test_an_unconfirmed_update_says_what_happened(connected, running, previous, expected):
    assert em_ota.failed_update_outcome(connected, running, previous) == expected


def test_the_update_decides_with_the_live_connection():
    """The verdict is only right if it is asked whether the device is back."""
    fn = _sources.def_source("em_ota._run_update_locked")
    call = next(n for n in ast.walk(ast.parse(fn)) if isinstance(n, ast.Call)
                and ast.unparse(n.func).endswith("failed_update_outcome"))
    assert ast.unparse(call.args[0]) == "device_id in em_state.devices"
    assert "auto-rolled back" in fn.split('outcome == "rolled_back"', 1)[1], \
        "the rollback message must sit under the rolled_back branch"
