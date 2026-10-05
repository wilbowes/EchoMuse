"""
The two places a config value reaches the running controller.

Some config keys are consumed by the CONTROLLER rather than the device — the
output chain, the wake threshold, the barge settings. For those, the JSON sent
down the wire is not what makes them work; an attribute on the Device object
is. That attribute is set in two places, and both have to agree:

  - `em_controller.handle_control`, when a device registers, and
  - `em_api._apply_live_config`, when someone saves config in the dashboard.

A key mirrored in the first but not the second reads as working. The database
is written, the device is sent the value and throws it away, and the setting
takes effect the next time the device happens to reconnect — so it works after
a restart, which is exactly when someone would stop investigating.

That is what happened to the limiter and the bass guard (2026-08-19): a whole
listening test produced no audible difference from any setting, because none
of them were reaching the audio. `_apply_live_config`'s own docstring warned
about this shape — "a mirror added to one but not the other is a bug that
reads as working" — and it happened anyway, which is why it is now a test
rather than a comment.
"""

import re
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]

# Keys deliberately mirrored only at registration.
#
# startupVolume is device STATE, not a setting (em_config_sections.STATE_KEYS):
# it seeds the device's level in the window before its first volume_state
# report, and a later push must NOT stomp a volume somebody has since changed
# by hand.
REGISTRATION_ONLY = {"startupVolume"}


def _registration_keys() -> set[str]:
    src = (CONTROLLER / "em_controller.py").read_text()
    block = src.split('await device.send_control({"type": "config", **config})')
    assert len(block) > 1, "registration config push not found — has it moved?"

    # Bounded by the END OF THE ENCLOSING METHOD, not by a byte count.
    #
    # This used to take a fixed 2500-character window, which is a slow leak
    # dressed as a parser: every comment added inside handle_control pushes
    # the last mirror closer to the edge, and the key that falls off the end
    # silently LEAVES this set. `missing = reg - live` then shrinks, the test
    # goes greener, and the coverage is gone with nothing saying so — which
    # is the exact shape this file exists to catch, turned on the file
    # itself. Found 2026-08-22 when four added lines pushed limiterRelease
    # over the boundary; that direction failed loudly, the other would not
    # have.
    tail = block[1]
    end = re.search(r"\n {0,4}(async )?def ", tail)
    body = tail[:end.start()] if end else tail
    return set(re.findall(r'config\.get\(\s*["\']([A-Za-z]+)["\']', body))


def _live_push_keys() -> set[str]:
    src = (CONTROLLER / "em_api.py").read_text()
    block = src.split("async def _apply_live_config")
    assert len(block) > 1, "_apply_live_config not found — has it been renamed?"
    body = block[1].split("\n@auth")[0]
    return set(re.findall(r'effective\[?["\']([A-Za-z]+)["\']', body))


def test_every_registration_mirror_is_also_applied_on_a_live_push():
    reg = _registration_keys()
    live = _live_push_keys()
    missing = reg - live - REGISTRATION_ONLY
    assert not missing, (
        "These config keys update the controller when a device registers but "
        "NOT when config is saved:\n  " + "\n  ".join(sorted(missing)) +
        "\n\nThey will appear to do nothing until the device reconnects. Add "
        "them to em_api._apply_live_config, or to REGISTRATION_ONLY with a "
        "reason."
    )


def test_the_output_chain_is_carried_by_both():
    """
    Named explicitly, because these five are the ones with no device-side
    fallback at all: the firmware ignores them, so the controller-side mirror
    is the ONLY thing that makes them work.
    """
    chain = {"limiterEnabled", "limiterThreshold", "limiterRelease",
             "bassGuardEnabled", "bassGuardDb"}
    assert chain <= _registration_keys()
    assert chain <= _live_push_keys()


def test_registration_only_keys_are_really_absent():
    """
    Keeps the exemption list honest: an entry that IS mirrored live should be
    removed from it rather than sitting there implying a rule that no longer
    holds.
    """
    stale = REGISTRATION_ONLY & _live_push_keys()
    assert not stale, (
        f"REGISTRATION_ONLY lists {sorted(stale)}, but they are applied on a "
        f"live push — drop them from the exemption")
