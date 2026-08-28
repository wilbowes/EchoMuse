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
# capabilities() moved out of control.go and into a per-board file
# (2026-08-26, crown support): capabilities_default.go for every build that
# isn't explicitly another board (`!crown` — same fallback-to-biscuit
# philosophy as internal/bindings/led), capabilities_crown.go for `-tags
# crown`. capabilities_default.go (biscuit) remains the ground truth for the
# device<->controller typo-guard below, since every string it checks against
# `em_controller.py`/`em_esphome.py` is one biscuit sends.
#
# crown is NOT a strict subset, though: it sends "display" (a screen-icon
# hint, checked by dashboard.jsx, not by the two Python idioms this file
# scans), which biscuit never sends at all. That string used to be invisible
# to this whole test file — found 2026-08-27, one PR review after it shipped
# — so `crown_capabilities()`/the display-specific test below exist
# specifically to pin it, the same way every other capability is pinned in
# both directions.
CONTROL_GO = ROOT / "device" / "internal" / "client" / "capabilities_default.go"
CONTROL_GO_CROWN = ROOT / "device" / "internal" / "client" / "capabilities_crown.go"
CONTROLLER = ROOT / "controller" / "em_controller.py"
API = ROOT / "controller" / "em_api.py"
ESPHOME = ROOT / "controller" / "em_esphome.py"


def _read_capabilities(path: Path) -> list[str]:
    src = path.read_text()
    m = re.search(r'func capabilities\(\) \[\]string \{(.*?)\n\}', src, re.S)
    assert m, f"could not find func capabilities() in {path.name}"
    return re.findall(r'"([a-z_]+)"', m.group(1))


def device_capabilities() -> list[str]:
    """
    Every capability biscuit's firmware can announce.

    Read from the whole capabilities() function rather than a single literal:
    the list is no longer fixed — "ambient_light" is appended only when the
    hardware actually has a readable sensor, because the controller advertises
    an HA entity off the back of it. A parser that only understood one literal
    would silently stop covering the conditional ones, which is the direction
    that hides a typo rather than surfacing it.
    """
    return _read_capabilities(CONTROL_GO)


def crown_capabilities() -> list[str]:
    """Every capability crown's firmware can announce. See the module
    comment above — this exists because crown is not a subset of biscuit's
    list and nothing was reading this file before."""
    return _read_capabilities(CONTROL_GO_CROWN)


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


def test_crown_display_capability_is_pinned_both_directions():
    """
    "display" is crown-only (biscuit has no screen) and is checked by
    dashboard.jsx directly — `(device.capabilities || []).includes('display')`
    — not by either of the two Python idioms
    `test_every_capability_the_controller_checks_is_one_the_device_sends`
    scans for. That test therefore cannot see this capability at all in
    either direction, and neither can `device_capabilities()`, which only
    reads biscuit's file. A typo on either side — the Go string, or the JS
    literal — would silently leave the screen-bodied device icon dark
    forever, exactly the failure class every other capability is pinned
    against.
    """
    caps = crown_capabilities()
    assert "display" in caps, "crown firmware no longer announces display"
    jsx = (ROOT / "controller" / "static" / "dashboard.jsx").read_text()
    assert re.search(r"capabilities\s*\|\|\s*\[\]\)\.includes\('display'\)", jsx), \
        "dashboard.jsx must still gate the screen-bodied device icon on the display capability"


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
