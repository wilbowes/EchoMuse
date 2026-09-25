"""
What Home Assistant sees of a device's ears (#438, #286).

Read as source, not imported: em_esphome pulls in zeroconf and aiohttp, which
this suite deliberately does without. What these pin:

- The binary sensor for the button mute is READ-ONLY. A writable entity here
  would be a remote unmute, the one thing the mute button exists to make
  impossible — so nothing in the controller may accept a command for it.
- Turning the wake word off is HA's OWN picker, not an entity of ours. HA
  re-reads the satellite configuration only at setup and right after it
  writes one, so as the only control it always reads back what it set.
- The picker's write is applied BEFORE its handler yields, because HA reads
  the configuration back immediately behind it — a change still waiting on a
  task reads back as the old value and the picker snaps back.
- The choice is STORED (em_db), because HA restores the picker's state on its
  side but never sends it back; it shows whatever we report on reconnect.
- The button and the picker are INDEPENDENT: `mute_state` must not move the
  wake word, in either direction.
- The sensor is gated on `mic`, like every entity.
- Keys are append-only: HA keys its registry on them.
- Subscribing to states yields the sensor, or HA shows "unknown" until the
  next device event — which for the button mute could be never.
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
    """
    The mute button is worth having because software cannot undo it. Nothing
    that accepts a command from HA may name the sensor's key.
    """
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
    assert "_mute_state_msg()" in sub


def test_the_wake_word_defaults_to_listening():
    """A device nobody has told otherwise listens, so absent state reads ON.
    The mute defaults the other way: absent state is not muted."""
    src = ESPHOME.read_text()
    init = _block(src, "class DeviceESPhomeServer", "def set_capabilities")
    assert re.search(r"^\s{8}self\.wake_word_enabled\s*:\s*bool\s*=\s*True", init, re.M)
    assert re.search(r"^\s{8}self\.muted\s*:\s*bool\s*=\s*False", init, re.M)
    assert "return False, True" in _block(src, "def get_mute_and_wake(", "\n\n\n")
    db = DB.read_text()
    getter = _block(db, "def get_wake_word_enabled(", "\n\n\n")
    assert "not in _wake_word_off_ids" in getter, "stored as the OFF set, so absent is on"


def test_the_picker_reads_back_the_truth():
    """
    HA's picker restores its own last state but never sends it back — on
    every read it reconciles to what we report. So the read side IS the
    display, and must never report the model while detection is off.
    """
    src = ESPHOME.read_text()
    cfg = _block(src, "isinstance(msg, api_pb2.VoiceAssistantConfigurationRequest)",
                 "\n        if isinstance(msg, ")
    assert "active_wake_words=[self.oww_model_id] if enabled else []" in cfg
    assert "wake_word_enabled" in cfg


def test_the_picker_turns_detection_on_and_off():
    """
    The write reads its meaning from em_wakeword.requested_on (membership,
    because HA sends the union of two pickers), declines a list naming only
    wake words we lack, and otherwise applies it.
    """
    src = ESPHOME.read_text()
    setcfg = _block(src, "isinstance(msg, api_pb2.VoiceAssistantSetConfiguration)",
                    "\n        if isinstance(msg, ")
    assert "em_wakeword.requested_on(requested, self.oww_model_id)" in setcfg
    assert "if want is None:" in setcfg
    assert "self._apply_wake_word(want)" in setcfg


def test_the_picker_write_is_applied_before_the_handler_yields():
    """
    HA sends VoiceAssistantConfigurationRequest straight behind its write
    (`_update_satellite_config`). If the new state were only set inside a
    task, the read-back would report the old one and the picker would snap
    back to it — so neither the handler nor the controller's setter may put
    the state change behind an await or a task.
    """
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
                 "db.set_wake_word_enabled(_d.device_id, t.enabled)"):
        assert line in setter, f"{line!r} must run before the setter returns"
    assert "await " not in setter


def test_the_choice_survives_a_controller_restart():
    """
    HA does not re-send its restored choice, so held only in memory, the
    wake word would come back on at every controller restart. The stored copy is read
    at connect and handed to the ESPHome server BEFORE its port comes up —
    HA can read the configuration the moment it does.
    """
    src = CONTROLLER.read_text()
    assert "device.wake_word_enabled = await loop.run_in_executor(\n            None, db.get_wake_word_enabled, device_id" in src
    connect = _block(src, "await esphome.device_connected(", "\n        )")
    assert "wake_word_enabled=device.wake_word_enabled" in connect
    esp = ESPHOME.read_text()
    dc = _block(esp, "async def device_connected(", "\nasync def device_disconnected(")
    assert dc.index("server.wake_word_enabled = bool(wake_word_enabled)") < dc.index("await server.start(host)")


def test_the_choice_is_not_a_config_key():
    """
    Config POSTs replace the stored dict with what the dashboard last loaded,
    so a key here would be written back stale by a dashboard left open, and a
    fleet value would switch every device at once. Only the picker writes it.
    """
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
    assert "esphome.update_mute_state(" in handler


def test_the_button_does_not_move_the_wake_word():
    """
    The review's ask, pinned where it can regress: `mute_state` may READ HA's
    choice — it has to, to know whether the stream the device just restarted
    is wanted — but it must never assign it. The two are independent.
    """
    src = CONTROLLER.read_text()
    handler = _block(src, 'msg_type == "mute_state"', 'msg_type == "volume_state"')
    assert "em_wakeword.on_hard_mute(" in handler
    assert not re.search(r"\.wake_word_enabled\s*=", handler)
    assert "esphome.update_wake_word(" not in handler
    assert "set_wake_word_enabled" not in handler


def test_unmuting_with_the_wake_word_off_takes_the_stream_back_down():
    """The device restarts its own wake stream on unmute; with the wake word
    off the controller would only discard those frames."""
    src = CONTROLLER.read_text()
    handler = _block(src, 'msg_type == "mute_state"', 'msg_type == "volume_state"')
    branch = _block(handler, "em_wakeword.on_hard_mute(", "if device.muted and")
    assert "mic_stop()" in branch


def test_the_wake_listener_honours_the_picker():
    """Both the frame gate and the stall watchdog of the stream path: a
    watchdog that only knows the button mute would restart the stream that
    turning the wake word off just stopped. The private path is its own test, below."""
    src = CONTROLLER.read_text()
    listener = _block(src, "async def _stream_listen(", "\nasync def ")
    assert listener.count("em_wakeword.wake_allowed(") >= 2


def test_the_wake_stream_does_not_come_up_with_the_wake_word_off():
    """
    Nine call sites restart the wake stream after something — a turn, an
    announcement, an alarm, a barge. Gating each one is nine places to
    forget, so `Device.mic_start` is the gate, the same way the device
    itself refuses `mic_start` under the button mute. `mic_start_turn` is
    deliberately not gated: an HA-initiated turn is HA's decision.
    """
    src = CONTROLLER.read_text()
    start = _block(src, "    async def mic_start(self):", "    async def mic_start_turn(self):")
    assert "wake_word_enabled" in start
    turn = _block(src, "    async def mic_start_turn(self):", "    async def mic_stop(self):")
    assert "wake_word_enabled" not in turn


def test_the_api_readout_is_the_stored_choice():
    """
    `/api/devices` reports the wake word for a device that is OFFLINE, so it
    is read from the stored choice, not defaulted off `live` like its
    neighbours (speaking/listening/thinking), which an offline device
    genuinely is not doing. A default there would report "listening" for a
    device HA turned off.
    """
    src = (ROOT / "controller" / "em_api.py").read_text()
    lines = [ln for ln in src.splitlines() if '"wake_word":' in ln]
    assert len(lines) == 1, lines
    assert "db.get_wake_word_enabled(" in lines[0], lines[0].strip()
    assert "if live else" not in lines[0], lines[0].strip()


def test_turning_the_wake_word_off_paints_nothing_on_the_ring():
    """
    #286 asked for no ring indicator and #66 is where one belongs — HA
    driving the idle ring can then show whatever the user wants. So the
    picker must not reach the LED paths at all, and `em_scenes` must not
    grow a colour for it that would later have to become configurable.
    """
    src = CONTROLLER.read_text()
    setter = _block(src, "def _set_wake_word(", "        # Capabilities before")
    assert "await leds_" not in setter
    assert "send_led_anim" not in setter and "set_leds" not in setter
    scenes = (ROOT / "controller" / "em_scenes.py").read_text()
    assert "wake_word" not in scenes
    for fn in ("async def leds_off(", "async def _leds_turn_end("):
        assert "wake_word_enabled" not in _block(src, fn, "\n\n\n")


def test_a_private_wake_is_declined_with_the_wake_word_off():
    """
    Under private listening (#602, the default) the Echo scores the wake
    word itself and sends `oww_wake` with a session. The stream-path gates
    never see that, and the mic_stop that turning it off sends ends neither
    the session nor local listening — so without a check here, a wake with
    the wake word off still starts a turn. It is closed with its own reason, so neither log
    blames the button.
    """
    src = CONTROLLER.read_text()
    turn = _block(src, "async def _private_wake_turn(", "\n\n\n")
    gate = turn.index("if not device.wake_word_enabled:")
    close = turn.index('await device.listen_close(session, "wake_off")', gate)
    assert close < turn.index("Wake word detected"), \
        "the wake must be declined before a turn is set up"
    assert turn.index('listen_close(session, "muted")') < gate, \
        "the button mute keeps its own reason and is checked first"
