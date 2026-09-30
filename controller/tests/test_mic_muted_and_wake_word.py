"""
What Home Assistant sees of a device's microphone (#438) and wake word (#286).
Read as source: em_esphome pulls in zeroconf and aiohttp, which this suite
does without.

- "Microphone Muted" is the physical button, READ-ONLY (a writable entity
  would be a remote unmute), gated on `mic`, and reported on subscribe.
- Turning the wake word off is HA's own picker, applied before the handler
  yields (HA reads it straight back) and stored (HA never sends it back).
- The button and the picker are independent, and neither is the media
  player's output mute (em_output_mute).
- Entity keys are append-only: HA keys its registry on them.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ESPHOME = ROOT / "controller" / "em_esphome.py"
CONTROLLER = ROOT / "controller" / "em_controller.py"
DB = ROOT / "controller" / "em_db.py"


def _block(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


def test_entity_keys_are_appended_not_renumbered():
    src = ESPHOME.read_text()
    assert re.search(r"^MEDIA_PLAYER_KEY\s*=\s*1\b", src, re.M)
    assert re.search(r"^EVENT_KEY\s*=\s*2\b", src, re.M)
    assert re.search(r"^AMBIENT_LUX_KEY\s*=\s*3\b", src, re.M)
    assert re.search(r"^MIC_MUTED_KEY\s*=\s*4\b", src, re.M)


def test_the_mute_sensor_is_advertised_and_gated_on_mic():
    src = ESPHOME.read_text()
    entities = _block(src, "isinstance(msg, api_pb2.ListEntitiesRequest)",
                      "ListEntitiesDoneResponse()")
    sensor = _block(entities, "ListEntitiesBinarySensorResponse(", ")")
    assert "key=MIC_MUTED_KEY" in sensor
    assert "self.label" not in sensor
    # Gated: the yield sits under the mic check, not at the top level.
    gate = entities.index("if self._mic_capable:")
    assert gate < entities.index("ListEntitiesBinarySensorResponse(")


def test_the_button_mute_is_read_only():
    """Nothing that takes a command from HA may name the sensor's key."""
    src = ESPHOME.read_text()
    for handler in ("isinstance(msg, api_pb2.VoiceAssistantSetConfiguration)",
                    "isinstance(msg, api_pb2.MediaPlayerCommandRequest)"):
        block = _block(src, handler, "\n        if isinstance(msg, ")
        assert "MIC_MUTED_KEY" not in block
    assert src.count("MIC_MUTED_KEY") == 3, "defined, advertised, reported — nothing else"


def test_subscribing_to_states_reports_the_mute():
    src = ESPHOME.read_text()
    sub = _block(src, "isinstance(msg, (api_pb2.SubscribeStatesRequest,",
                 "SubscribeVoiceAssistantRequest")
    assert "_mic_muted_msg()" in sub


def test_the_wake_word_defaults_to_listening():
    """Absent state reads as listening and not mic-muted."""
    src = ESPHOME.read_text()
    init = _block(src, "class DeviceESPhomeServer", "def set_capabilities")
    assert re.search(r"^\s{8}self\.wake_word_enabled\s*:\s*bool\s*=\s*True", init, re.M)
    assert re.search(r"^\s{8}self\.mic_muted\s*:\s*bool\s*=\s*False", init, re.M)
    # The speaker's mute is a different thing and keeps its own state.
    assert "self.output_mute = em_output_mute.OutputMute()" in init
    assert "return False, True" in _block(src, "def get_mic_muted_and_wake_word(", "\n\n\n")
    db = DB.read_text()
    getter = _block(db, "def get_wake_word_enabled(", "\n\n\n")
    assert "not in _wake_word_off_ids" in getter, "stored as the OFF set, so absent is on"


def test_the_picker_reads_back_the_truth():
    """HA's picker shows what we report, so it must never report the model while off."""
    src = ESPHOME.read_text()
    cfg = _block(src, "isinstance(msg, api_pb2.VoiceAssistantConfigurationRequest)",
                 "\n        if isinstance(msg, ")
    assert "active_wake_words=[self.oww_model_id] if enabled else []" in cfg
    assert "wake_word_enabled" in cfg


def test_the_picker_turns_detection_on_and_off():
    """Meaning comes from em_wakeword.requested_on; a decline is logged, not applied."""
    src = ESPHOME.read_text()
    setcfg = _block(src, "isinstance(msg, api_pb2.VoiceAssistantSetConfiguration)",
                    "\n        if isinstance(msg, ")
    assert "em_wakeword.requested_on(requested, self.oww_model_id)" in setcfg
    assert "if want is None:" in setcfg
    assert "self._apply_wake_word(want)" in setcfg


def test_the_picker_write_is_applied_before_the_handler_yields():
    """HA reads the configuration back straight after writing it, so no state change may wait on an await or a task."""
    src = ESPHOME.read_text()
    setcfg = _block(src, "isinstance(msg, api_pb2.VoiceAssistantSetConfiguration)",
                    "\n        if isinstance(msg, ")
    assert "create_task" not in setcfg and "await " not in setcfg
    apply = _block(src, "    def _apply_wake_word(self, on: bool) -> None:", "\n    def ")
    assert "create_task" not in apply
    assert re.search(r"^\s+set_fn\(on\)\s*$", apply, re.M), "called, not scheduled"

    ctrl = CONTROLLER.read_text()
    setter = _block(ctrl, "        def _set_wake_word(on: bool, _d=_device_ref) -> None:",
                    "            async def _follow()")
    for line in ("_d.wake_word_enabled = t.enabled",
                 "esphome.update_wake_word(_d.device_id, t.enabled)",
                 "em_dbwriter.submit(db.set_wake_word_enabled, _d.device_id, t.enabled)"):
        assert line in setter, f"{line!r} must run before the setter returns"
    assert "await " not in setter


def test_the_choice_survives_a_controller_restart():
    """Read from the DB at connect and set on the server before its port comes up."""
    src = CONTROLLER.read_text()
    assert "device.wake_word_enabled = await loop.run_in_executor(\n            None, db.get_wake_word_enabled, device_id" in src
    connect = _block(src, "await esphome.device_connected(", "\n        )")
    assert "wake_word_enabled=device.wake_word_enabled" in connect
    esp = ESPHOME.read_text()
    dc = _block(esp, "async def device_connected(", "\nasync def device_disconnected(")
    assert dc.index("server.wake_word_enabled = bool(wake_word_enabled)") < dc.index("await server.start(host)")


def test_the_choice_is_not_a_config_key():
    """A dashboard save would write a stale value back, and a fleet value would switch every device."""
    db = DB.read_text()
    defaults = _block(db, "DEFAULT_DEVICE_CONFIG", "\n}\n")
    assert "akeWord" not in defaults
    sections = (ROOT / "controller" / "em_config_sections.py").read_text()
    assert "akeWordEnabled" not in sections


def test_deleting_a_device_forgets_its_choice():
    db = DB.read_text()
    delete = _block(db, "def delete_device(", "\n\n\n")
    assert "set_wake_word_enabled(device_id, True)" in delete


def test_the_controller_reports_the_button_mute_to_ha():
    src = CONTROLLER.read_text()
    handler = _block(src, 'msg_type == "mute_state"', 'msg_type == "volume_state"')
    assert "esphome.update_mic_muted(" in handler


def test_the_button_does_not_move_the_wake_word():
    """`mute_state` may read HA's choice but never assign it."""
    src = CONTROLLER.read_text()
    handler = _block(src, 'msg_type == "mute_state"', 'msg_type == "volume_state"')
    assert "em_wakeword.on_mic_mute(" in handler
    assert not re.search(r"\.wake_word_enabled\s*=", handler)
    assert "esphome.update_wake_word(" not in handler
    assert "set_wake_word_enabled" not in handler


def test_unmuting_with_the_wake_word_off_takes_the_stream_back_down():
    """The device restarts its wake stream on unmute; with the wake word off it goes back down."""
    src = CONTROLLER.read_text()
    handler = _block(src, 'msg_type == "mute_state"', 'msg_type == "volume_state"')
    branch = _block(handler, "em_wakeword.on_mic_mute(", "if device.muted and")
    assert "mic_stop()" in branch


def test_the_wake_listener_honours_the_picker():
    """Both the frame gate and the stall watchdog, or the watchdog restarts the stream."""
    src = CONTROLLER.read_text()
    listener = _block(src, "async def _stream_listen(", "\nasync def ")
    assert listener.count("em_wakeword.wake_allowed(") >= 2


def test_the_wake_stream_does_not_come_up_with_the_wake_word_off():
    """`Device.mic_start` is the one gate; `mic_start_turn` (an HA-initiated turn) is not gated."""
    src = CONTROLLER.read_text()
    start = _block(src, "    async def mic_start(self):", "    async def mic_start_turn(self):")
    assert "wake_word_enabled" in start
    turn = _block(src, "    async def mic_start_turn(self):", "    async def mic_stop(self):")
    assert "wake_word_enabled" not in turn


def test_the_api_readout_is_the_stored_choice():
    """Read from the DB, not defaulted off `live`, so an offline device reports HA's choice."""
    src = (ROOT / "controller" / "em_api.py").read_text()
    lines = [ln for ln in src.splitlines() if '"wake_word":' in ln]
    assert len(lines) == 1, lines
    assert "db.get_wake_word_enabled(" in lines[0], lines[0].strip()
    assert "if live else" not in lines[0], lines[0].strip()


def test_turning_the_wake_word_off_paints_nothing_on_the_ring():
    """No ring indicator (#286); HA driving the idle ring is #66."""
    src = CONTROLLER.read_text()
    setter = _block(src, "def _set_wake_word(", "        # Capabilities before")
    assert "await leds_" not in setter
    assert "send_led_anim" not in setter and "set_leds" not in setter
    scenes = (ROOT / "controller" / "em_scenes.py").read_text()
    assert "wake_word" not in scenes
    for fn in ("async def leds_off(", "async def _leds_turn_end("):
        assert "wake_word_enabled" not in _block(src, fn, "\n\n\n")


def test_a_private_wake_is_declined_with_the_wake_word_off():
    """The Echo scores its own wake word (#602), so each wake is declined, after the mic mute check and with its own reason."""
    src = CONTROLLER.read_text()
    turn = _block(src, "async def _private_wake_turn(", "\n\n\n")
    gate = turn.index("if not device.wake_word_enabled:")
    close = turn.index('await device.listen_close(session, "wake_off")', gate)
    assert close < turn.index("Wake word detected"), \
        "the wake must be declined before a turn is set up"
    assert turn.index('listen_close(session, "muted")') < gate, \
        "the button mute keeps its own reason and is checked first"
