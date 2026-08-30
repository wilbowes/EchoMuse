"""
Device/controller compatibility is negotiated by CAPABILITY, not version.

The two halves of this project ship on independent version schemes (device
`v*`, controller `controller-v*`), so any given moment can pair new firmware
with an old controller or the reverse. Version comparison would mean encoding
release history into the controller and getting it wrong the first time
someone runs a dev build; a capability is the device stating what it
implements.

That only works if both sides spell the capability identically. A typo makes
the feature permanently unavailable and looks exactly like a device that does
not support it — silent, and the sort of thing you debug from the wrong end.
So the strings are asserted to match across the two languages, the same way
CONFIG_SECTIONS is mirrored between Python and dashboard.jsx.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONTROL_GO = ROOT / "device" / "internal" / "client" / "control.go"
CONTROLLER = ROOT / "controller" / "em_controller.py"
API = ROOT / "controller" / "em_api.py"
ESPHOME = ROOT / "controller" / "em_esphome.py"


def device_capabilities() -> list[str]:
    """
    Every capability the firmware can announce.

    Read from the whole capabilities() function rather than a single literal:
    the list is no longer fixed — "ambient_light" is appended only when the
    hardware actually has a readable sensor, because the controller advertises
    an HA entity off the back of it. A parser that only understood one literal
    would silently stop covering the conditional ones, which is the direction
    that hides a typo rather than surfacing it.
    """
    src = CONTROL_GO.read_text()
    m = re.search(r'func capabilities\(\) \[\]string \{(.*?)\n\}', src, re.S)
    assert m, "could not find func capabilities() in control.go"
    return re.findall(r'"([a-z_]+)"', m.group(1))


def test_device_announces_expected_capabilities():
    caps = device_capabilities()
    for expected in ("mic", "speaker", "leds", "led_anim", "buttons", "oww_shadow"):
        assert expected in caps, f"firmware no longer announces {expected!r}"


def test_every_capability_the_controller_checks_is_one_the_device_sends():
    """
    A controller checking for a capability string the device never sends is a
    feature that is silently off forever. This catches the typo direction that
    the device-side test cannot.
    """
    caps = set(device_capabilities())
    # Two idioms, both scanned: `"<cap>" in (self.capabilities or [])` on
    # Device in em_controller, and _device_has("<cap>") on the ESPHome
    # satellite, which decides which HA entities get advertised. A typo in
    # either is an entity that never appears or never fires, with no error
    # anywhere — exactly what this test exists to catch.
    checked = set(re.findall(r'"([a-z_]+)"\s+in\s+\(self\.capabilities',
                             CONTROLLER.read_text()))
    checked |= set(re.findall(r'_device_has\(\s*"([a-z_]+)"\s*\)',
                              ESPHOME.read_text()))
    assert checked, "no capability checks found — has the idiom changed?"
    unknown = checked - caps
    assert not unknown, (
        f"controller checks capabilities the firmware never announces: {sorted(unknown)}. "
        f"Device sends: {sorted(caps)}"
    )


def test_shadow_capability_is_surfaced_to_the_dashboard():
    """
    The dashboard must be able to tell "cannot" from "off", or it offers a
    toggle that silently does nothing on older firmware — which reads as a
    broken feature rather than an unsupported one.
    """
    assert "oww_shadow_capable" in CONTROLLER.read_text(), \
        "em_controller must expose the shadow capability as a property"
    assert "owwShadowCapable" in API.read_text(), \
        "/api/devices must surface the shadow capability"
    jsx = (ROOT / "controller" / "static" / "dashboard.jsx").read_text()
    assert "owwShadowCapable" in jsx, \
        "the dashboard must gate the on-device toggle on the capability"


def test_triggering_is_a_separate_capability_from_scoring():
    """
    Shadow shipped first, so there is firmware in the field that scores the
    wake word and reports it while having no code to act on it. Gating "on"
    behind oww_shadow alone would offer those devices a mode that leaves them
    scoring perfectly and never answering — the "I enabled it and nothing
    happened" the capability rule exists to prevent.
    """
    caps = device_capabilities()
    assert "oww_trigger" in caps, "firmware no longer announces oww_trigger"
    assert "oww_shadow" in caps, \
        "oww_trigger must not replace oww_shadow — shadow is still a mode"
    assert "oww_trigger_capable" in CONTROLLER.read_text(), \
        "em_controller must expose the trigger capability as a property"
    assert "owwTriggerCapable" in API.read_text(), \
        "/api/devices must surface the trigger capability"
    assert "owwTriggerCapable" in (ROOT / "controller" / "static" / "dashboard.jsx").read_text(), \
        "the dashboard must gate the 'On device' option on the capability"


def test_the_toggle_control_actually_honours_disabled():
    """
    "Disabled WITH the reason, never a control that silently does nothing" is
    the rule every capability gate above is enforced by — and Toggle did not
    accept a `disabled` prop at all; only Slider did. So the rule could not be
    expressed on a switch, and the one call site that needed it ("Tap sends an
    event", gated on button_hold) had to fake it by neutering `value` and
    `onChange` by hand — which leaves the control looking live: full-contrast
    label, pointer cursor, a switch that animates when clicked and stores
    nothing. Any caller passing `disabled` in good faith got worse: a switch
    greyed with its reason that WROTE THE OPPOSITE VALUE on click, so the
    stored setting silently disagreed with what the control showed.

    Asserted against the component rather than the call sites, because the
    call sites already looked correct while the bug was live.
    """
    jsx = (ROOT / "controller" / "static" / "dashboard.jsx").read_text()
    m = re.search(r'function Toggle\(\{(.*?)\}\)', jsx, re.S)
    assert m, "dashboard.jsx must still define a Toggle component"
    assert "disabled" in m.group(1), \
        "Toggle must accept a `disabled` prop — every caller that passes one " \
        "is relying on it to refuse the write, not merely to grey the switch"

    body = jsx[m.end():jsx.index("\n}", m.end())]
    assert re.search(r'if\s*\(!disabled\)|disabled\s*\?\s*undefined|disabled\s*\|\|', body), \
        "Toggle's click handler must check `disabled` before calling onChange — " \
        "styling it grey while still writing the value is the bug this pins"


def test_capabilities_reported_before_the_server_exists_are_not_lost():
    """
    A device registers BEFORE its ESPHome server is created — the listener
    only comes up once the device is present — so the capability push finds
    no server. Dropping it there built the entity list from an empty set, and
    that list is a ONE-SHOT at ListEntities time: HA caches it and the sensor
    is absent for the life of the connection.

    Observed on Retreat, 2026-08-03: registered 05:25:33, server created
    05:25:34, no ambient light entity in HA afterwards. The same race resolves
    differently on each controller restart, which is why the graph came and
    went rather than simply never working.

    Read as source, not imported: em_esphome pulls in zeroconf and aiohttp,
    which this suite deliberately does without.
    """
    src = ESPHOME.read_text()
    setter = re.search(r"def set_device_capabilities\(.*?\n(?=\n\ndef |\Z)", src, re.S)
    assert setter, "could not find set_device_capabilities"
    body = setter.group(0)
    # The store must happen unconditionally — not inside the "if server" arm,
    # which is exactly what dropped it.
    store = re.search(r"^\s{4}_pending_caps\[device_id\]\s*=", body, re.M)
    assert store, (
        "set_device_capabilities must hold the capabilities unconditionally; "
        "pushing them only when a server already exists loses them"
    )


def test_the_pending_capabilities_are_applied_when_the_server_is_built():
    """
    Guard against the two halves drifting: holding the value is only useful
    if server creation applies it to the same attribute the ListEntities gate
    reads (_device_has → srv.capabilities).
    """
    src = ESPHOME.read_text()
    create = re.search(r"async def _register_device_server\(.*?\n(?=\n\nasync def |\n\ndef |\Z)",
                       src, re.S)
    assert create, "could not find _register_device_server"
    body = create.group(0)
    assert "_pending_caps" in body, \
        "server creation must seed capabilities from the pending map"
    assert "set_capabilities" in body, \
        "and must apply them via the same setter the entity gate reads"


def test_a_capability_that_changes_later_rebuilds_the_entity_list():
    """
    The other half of the one-shot problem, and `_pending_caps` does not reach
    it: when capabilities change AFTER HA has enumerated, the server has the
    new list but HA read the entity list once at connect and never asks again.

    Reachable in normal operation rather than only in theory. `als.resolve()`
    deliberately does not cache a negative result, because the first lookup
    happens moments after a cold boot when sysfs is least likely to be
    complete — so a device can register without `ambient_light` and acquire it
    on a later scan. Before this, that device kept the entity list from the
    registration that missed the sensor, and the only cure was a controller
    restart that happened to win the race (#90).

    Bouncing the HA connection is the documented remedy; `update_oww_model`
    does the same for the wake word configuration, and HA redials in seconds.
    """
    src = ESPHOME.read_text()
    setter = re.search(r"def set_device_capabilities\(.*?\n(?=\n\ndef |\Z)", src, re.S)
    assert setter, "could not find set_device_capabilities"
    body = setter.group(0)

    assert "disconnect()" in body, (
        "a capability change after ListEntities must bounce the HA connection, "
        "or the new entity never appears for the life of that connection"
    )
    # It must be conditional on an actual change. Bouncing on every register
    # would drop HA's connection on every device reconnect.
    assert re.search(r"if\s+set\(caps\)\s*==\s*before", body), (
        "the bounce must be gated on the capability set actually changing; "
        "bouncing unconditionally disconnects HA on every device reconnect"
    )


def test_the_api_can_tell_no_sensor_from_no_reading():
    """
    0 lux is a real reading from a covered sensor, so absence cannot be
    expressed as a value: whether the device HAS the sensor has to be a
    separate field from what it read.

    Asserted on the API rather than the dashboard deliberately. A first
    attempt put this on the Status tab, which pushed the panel past its
    height and gave the page a scrollbar — what the device panel should show
    is its own question, tracked separately. The API field stands on its own
    merits regardless of who renders it.
    """
    api = (Path(__file__).resolve().parent.parent / "em_api.py").read_text()
    assert "ambientLightCapable" in api, \
        "/api/devices must report whether the device found its ALS"


def test_hardware_echo_reference_is_a_capability_and_a_runtime_state():
    """
    Two facts, and neither substitutes for the other.

    `aec_hw_ref` says the FIRMWARE knows how to take the AEC far-end
    reference from a playback loopback in the mic capture. `aecRef` on the
    stats report says whether the board turned out to have one. They must
    stay separate because the proof is only available at runtime: confirming
    a channel is a loopback requires it to be bit-exact silent at idle AND
    to carry audio while the speaker plays, and nothing has played at
    registration time. A controller that read the capability as "it is using
    a hardware reference" would disable the AEC delay slider on every device
    the moment it connected, including the ones that fall back to the
    software tap and need that slider.
    """
    caps = device_capabilities()
    assert "aec_hw_ref" in caps, "firmware no longer announces aec_hw_ref"

    ctl = CONTROLLER.read_text()
    assert "aec_hw_ref_capable" in ctl, \
        "em_controller must expose the hardware-reference capability"
    assert '"aecRef"' in ctl or "'aecRef'" in ctl, \
        "the stats allowlist must carry aecRef, or it is dropped in the relay"

    api = API.read_text()
    for field in ("aecHwRefCapable", "aecRef"):
        assert field in api, f"/api/devices must surface {field}"

    jsx = (ROOT / "controller" / "static" / "dashboard.jsx").read_text()
    assert "hwEchoRef" in jsx, \
        "the dashboard must gate the AEC delay control on the live reference"


def test_the_aec_delay_control_is_gated_on_the_live_reference_not_the_capability():
    """
    The delay slider compensates write-to-ear latency for the software tap.
    On the frame-aligned hardware reference there is nothing to compensate,
    so it must be shown disabled with the reason — but only for a device
    actually running on it.

    Gating on `aecHwRefCapable` instead would disable the slider on every
    device with current firmware, including one whose board has no loopback,
    leaving a real control permanently greyed out with a reason that is not
    true of it.
    """
    jsx = (ROOT / "controller" / "static" / "dashboard.jsx").read_text()
    m = re.search(r"hwEchoRef=\{([^}]*)\}", jsx)
    assert m, "dashboard must pass hwEchoRef to the config form"
    expr = m.group(1)
    assert "aecRef" in expr, \
        "hwEchoRef must come from the runtime aecRef, not from the capability"
    assert "aecHwRefCapable" not in expr, (
        "hwEchoRef must NOT be driven by the capability: a capable device on "
        "the software tap still needs its delay slider"
    )
