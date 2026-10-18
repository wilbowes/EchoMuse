"""
Ring brightness (#295): one device config key, applied at render, plus the
Home Assistant number entity that writes it.

em_esphome pulls in aiohttp and zeroconf, so its side is read as source, the
way the mute sensor's guard is. The scoping and typing questions are asked of
the real modules — that is where the setting can silently stop working, and
where a source guard would only agree with itself.
"""

import _sources
import re
from pathlib import Path

import em_config_sections as S
import em_config_types as T
import em_db

ROOT = Path(__file__).resolve().parent.parent.parent
ESPHOME = ROOT / "controller" / "em_esphome.py"
CONTROLLER = ROOT / "controller" / "em_controller.py"


def _block(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


def test_the_key_is_a_ring_setting_typed_as_an_int():
    assert "ledBrightness" in S.SECTIONS["ring"]["keys"]
    assert T.KINDS["ledBrightness"] == T.INT
    assert em_db.DEFAULT_DEVICE_CONFIG["ledBrightness"] == 100


def test_a_per_device_value_applies_only_with_the_ring_section_overridden():
    fleet = dict(em_db.DEFAULT_DEVICE_CONFIG)
    own = {"ledBrightness": 30}
    assert S.merge(fleet, own, ["ring"])["ledBrightness"] == 30
    # The trap the HA write path must clear: a stored value in a section the
    # device inherits is ignored, so writing the key alone changes nothing.
    assert S.merge(fleet, own, [])["ledBrightness"] == 100


def test_the_entity_key_is_appended_not_renumbered():
    src = ESPHOME.read_text()
    assert re.search(r"^MEDIA_PLAYER_KEY\s*=\s*1\b", src, re.M)
    assert re.search(r"^MIC_MUTED_KEY\s*=\s*4\b", src, re.M)
    assert re.search(r"^LED_BRIGHTNESS_KEY\s*=\s*5\b", src, re.M)


def test_the_number_is_advertised_only_for_firmware_that_honours_it():
    src = ESPHOME.read_text()
    entities = _block(src, "isinstance(msg, api_pb2.ListEntitiesRequest)",
                      "ListEntitiesDoneResponse()")
    number = _block(entities, "ListEntitiesNumberResponse(", ")")
    assert "key=LED_BRIGHTNESS_KEY" in number
    assert 'unit_of_measurement="%"' in number
    assert "min_value=0.0" in number and "max_value=100.0" in number
    assert "step=1.0" in number
    assert entities.index("if self._led_brightness_capable:") \
        < entities.index("ListEntitiesNumberResponse(")


def test_ha_reads_the_state_on_subscribe_and_on_change():
    src = ESPHOME.read_text()
    sub = _block(src, "isinstance(msg, (api_pb2.SubscribeStatesRequest,",
                 "SubscribeVoiceAssistantRequest")
    assert "_led_brightness_msg()" in sub
    updater = _sources.def_source("update_led_brightness")
    assert "server.led_brightness = int(percent)" in updater
    assert "satellite._send_one(satellite._led_brightness_msg())" in updater


def test_a_command_rounds_the_float_and_echoes_what_was_accepted():
    src = ESPHOME.read_text()
    cmd = _block(src, "isinstance(msg, api_pb2.NumberCommandRequest)",
                 "\n        if isinstance(msg, api_pb2.SubscribeHomeassistantServicesRequest)")
    assert "msg.key != LED_BRIGHTNESS_KEY" in cmd, "commands for keys we did not declare"
    # HA sends a float. The stored key is an INT, and the device drops the
    # whole config message on a type mismatch, so the rounding must happen
    # before the value is stored — not in the device.
    assert "int(round(msg.state))" in cmd
    assert "_apply_led_brightness(" in cmd
    # The echo is how HA learns the value was clamped.
    assert cmd.count("_led_brightness_msg()") == 1


def test_the_command_is_applied_before_the_handler_yields():
    apply = _sources.def_source("_apply_led_brightness")
    assert "create_task" not in apply and "await " not in apply
    assert re.search(r"^\s+set_fn\(percent\)\s*$", apply, re.M)


def test_ha_writes_are_stored_as_a_ring_override_and_pushed_live():
    setter = _sources.def_source("_set_led_brightness")
    assert "max(0, min(100, int(percent)))" in setter
    assert "esphome.update_led_brightness(_d.device_id, percent)" in setter
    follow = _block(setter, "async def _follow()", "em_tasks.spawn(_follow())")
    assert 'if "ring" not in sections:' in follow
    assert "db.set_device_config_sections" in follow
    assert '"ledBrightness": percent' in follow
    assert "em_device_config.apply_live_config(" in follow
    assert '"device_update"' in follow


def test_the_registration_seed_and_the_live_push_agree():
    ctrl = CONTROLLER.read_text()
    connect = _block(ctrl, "await esphome.device_connected(", "\n        )")
    assert "set_led_brightness=_set_led_brightness" in connect
    assert 'led_brightness=int(config.get("ledBrightness", 100))' in connect
    live = _sources.def_source("apply_live_config")
    assert 'effective["ledBrightness"]' in live
    assert "em_esphome.update_led_brightness(" in live


def test_the_device_is_told_the_stored_value_before_ha_can_read_it():
    dc = _sources.def_source("em_esphome.device_connected")
    assert dc.index("server.led_brightness = int(led_brightness)") \
        < dc.index("await server.start(host)")
    assert "server._set_led_brightness = None" in \
        _sources.def_source("em_esphome.device_disconnected")


def test_zero_is_expressible_on_the_wire():
    cfg = (ROOT / "device" / "internal" / "config" / "config.go").read_text()
    assert 'LedBrightness *int `json:"ledBrightness,omitempty"`' in cfg
    assert "if msg.LedBrightness != nil {" in cfg
