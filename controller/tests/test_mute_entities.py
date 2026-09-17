"""
The two mute entities Home Assistant sees (#438 read side, #286 write side).

Read as source, not imported: em_esphome pulls in zeroconf and aiohttp, which
this suite deliberately does without. What these pin:

- The binary sensor for the button mute is READ-ONLY. A writable entity here
  would be a remote unmute, the one thing the hard mute exists to make
  impossible — so nothing in the controller may accept a mute command for
  it, and the only writable entity is the soft mute's switch.
- Both entities are gated on `mic`, like every entity, so a device that
  cannot listen does not grow a switch that does nothing.
- Keys 4 and 5 are taken and stay taken: HA keys its registry on them.
- Subscribing to states yields both, or HA shows "unknown" until the next
  device event — which for the button mute could be never.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ESPHOME = ROOT / "controller" / "em_esphome.py"
CONTROLLER = ROOT / "controller" / "em_controller.py"


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
    assert re.search(r"^SOFT_MUTE_KEY\s*=\s*5\b", src, re.M)


def test_both_mute_entities_are_advertised_and_gated_on_mic():
    src = ESPHOME.read_text()
    entities = _block(src, "isinstance(msg, api_pb2.ListEntitiesRequest)",
                      "ListEntitiesDoneResponse()")
    sensor = _block(entities, "ListEntitiesBinarySensorResponse(", ")")
    switch = _block(entities, "ListEntitiesSwitchResponse(", ")")
    assert "key=MIC_MUTED_KEY" in sensor
    assert "key=SOFT_MUTE_KEY" in switch
    # Gated: the yield sits under the mic check, not at the top level.
    gate = entities.index("if self._mic_capable:")
    assert gate < entities.index("ListEntitiesBinarySensorResponse(")
    assert gate < entities.index("ListEntitiesSwitchResponse(")


def test_mute_entity_names_do_not_repeat_the_device_label():
    src = ESPHOME.read_text()
    entities = _block(src, "isinstance(msg, api_pb2.ListEntitiesRequest)",
                      "ListEntitiesDoneResponse()")
    for ctor in ("ListEntitiesBinarySensorResponse(", "ListEntitiesSwitchResponse("):
        assert "self.label" not in _block(entities, ctor, ")")


def test_the_button_mute_is_read_only():
    """
    The hard mute is worth having because software cannot undo it. The only
    command the controller accepts is the soft mute's switch, and it must
    not act on any other key.
    """
    src = ESPHOME.read_text()
    handler = _block(src, "isinstance(msg, api_pb2.SwitchCommandRequest)", "\n        if isinstance(msg, ")
    assert "SOFT_MUTE_KEY" in handler
    assert "MIC_MUTED_KEY" not in handler
    assert "_set_soft_mute" in handler


def test_subscribing_to_states_reports_both_mutes():
    src = ESPHOME.read_text()
    sub = _block(src, "isinstance(msg, (api_pb2.SubscribeStatesRequest,",
                 "SubscribeVoiceAssistantRequest")
    assert "_mute_state_msg()" in sub
    assert "_soft_mute_msg()" in sub


def test_the_soft_mute_survives_a_device_reconnect():
    """
    A Device is rebuilt per connection, so the soft mute must live on the
    server object that outlives it — the same place volume lives — and the
    hard state it is compared against on `mute_state` must too, or every
    reconnect reads as a button press and clears it.
    """
    src = ESPHOME.read_text()
    init = _block(src, "class DeviceESPhomeServer", "def set_capabilities")
    assert re.search(r"^\s{8}self\.soft_muted\s*:\s*bool\s*=\s*False", init, re.M)
    assert re.search(r"^\s{8}self\.muted\s*:\s*bool\s*=\s*False", init, re.M)


def test_the_controller_reports_the_button_mute_to_ha():
    src = CONTROLLER.read_text()
    handler = _block(src, 'msg_type == "mute_state"', 'msg_type == "volume_state"')
    assert "esphome.update_mute_state(" in handler
    assert "em_softmute.on_hard_mute(" in handler


def test_the_wake_listener_honours_the_soft_mute():
    """Both the frame gate and the stall watchdog: a watchdog that only knows
    the hard mute would restart the stream the soft mute just stopped."""
    src = CONTROLLER.read_text()
    listener = _block(src, "async def wake_word_listener(", "\nasync def ")
    assert listener.count("em_softmute.wake_allowed(") >= 2


def test_the_wake_stream_does_not_come_up_under_a_soft_mute():
    """
    Nine call sites restart the wake stream after something — a turn, an
    announcement, an alarm, a barge. Gating each one is nine places to
    forget, so `Device.mic_start` is the gate, the same way the device
    itself refuses `mic_start` under the hard mute. `mic_start_turn` is
    deliberately not gated: an HA-initiated turn is HA's decision.
    """
    src = CONTROLLER.read_text()
    start = _block(src, "    async def mic_start(self):", "    async def mic_start_turn(self):")
    assert "soft_muted" in start
    turn = _block(src, "    async def mic_start_turn(self):", "    async def mic_stop(self):")
    assert "soft_muted" not in turn


def test_the_idle_ring_shows_the_soft_mute():
    """
    `leds_off` is where every turn, announcement and alarm hands the ring
    back, so it is the one place the soft-mute colour needs to be — the
    same reasoning as `mic_start` for the stream. Painting it from each
    caller would be the nine-places-to-forget problem again.
    """
    src = CONTROLLER.read_text()
    off = _block(src, "async def leds_off(", "\n\n\n")
    assert "soft_muted" in off
    assert "soft_mute_anim" in off


def test_a_turn_end_cue_hands_back_to_the_soft_mute_colour():
    """The cue anims carry a 1s TTL and the device clears to BLACK when one
    expires, so after an unanswered ask_question a soft-muted ring would go
    dark and stay dark. The cue still plays; the colour comes back after."""
    src = CONTROLLER.read_text()
    end = _block(src, "async def _leds_turn_end(", "\n\n\n")
    assert "soft_muted" in end
