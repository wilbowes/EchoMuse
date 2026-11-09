"""
After a dashboard model switch, HA's wake word select must show the new model,
not "No wake word" (#1151).

HA's select keeps the option by the wake word's NAME. On the reconnect that
follows a switch the old name is no longer offered, so it falls back to "No
wake word"; it adopts the device's active wake word only when it is already on
"No wake word" (or unset). HA reads the configuration only at connect, so the
satellite bounces the connection a second time after that first read.

`ha_select_update` is HA's `EsphomeAssistSatelliteWakeWordSelect.
async_satellite_config_updated` for index 0 (homeassistant/components/esphome/
select.py, 2026.11.0), reduced to the option it picks. The satellite side is
the real handler answering the real request.
"""

import asyncio
from types import SimpleNamespace

import _oww_stub

_oww_stub.install()
try:
    import em_esphome
finally:
    _oww_stub.uninstall()

import em_oww_metadata  # noqa: E402
import em_wakeword  # noqa: E402
from esphome.vendor import api_pb2  # noqa: E402

NO_WAKE_WORD = "no_wake_word"


def ha_select_update(option, config) -> str:
    if not config.available_wake_words or config.max_active_wake_words < 1:
        return NO_WAKE_WORD
    wake_words = {w.wake_word: w.id for w in config.available_wake_words}
    if len(config.active_wake_words) == 1 and option in (None, NO_WAKE_WORD):
        option = next((n for n, i in wake_words.items()
                       if i == config.active_wake_words[0]), None)
    if (option is None or (wid := wake_words.get(option)) is None
            or wid not in config.active_wake_words):
        option = NO_WAKE_WORD
    return option


def satellite(server):
    s = em_esphome.EchoMuseSatellite(
        device_id="G090LF0000000001", label="Test", mac_address="02:EC:00:00:00:01",
        oww_model_id="hey_computer_v2",
        oww_model_info=em_oww_metadata.WakeWord("Hey Computer", ("en",)),
        on_disconnected_cb=lambda *_: None)
    s._owning_server = server
    s.bounces = 0
    s.disconnect = lambda: setattr(s, "bounces", s.bounces + 1)
    return s


def connect(server, option):
    """One HA connect: the configuration read, then whatever the satellite
    schedules. Returns HA's select option and whether a bounce followed."""
    async def run():
        s = satellite(server)
        (config,) = list(s.handle_message(api_pb2.VoiceAssistantConfigurationRequest()))
        await asyncio.sleep(em_wakeword.SELECT_REREAD_DELAY_S + 0.05)
        return ha_select_update(option, config), s.bounces
    return asyncio.run(run())


def test_the_old_name_alone_would_leave_the_select_on_no_wake_word():
    server = SimpleNamespace(wake_word_enabled=True, wake_word_told=None,
                             ha_select_stale=False)
    assert connect(server, "Hey Jarvis") == (NO_WAKE_WORD, 0)


def test_a_switch_with_the_wake_word_on_ends_on_the_new_model():
    server = SimpleNamespace(wake_word_enabled=True, wake_word_told=None,
                             ha_select_stale=True)
    option, bounces = connect(server, "Hey Jarvis")
    assert (option, bounces) == (NO_WAKE_WORD, 1)
    assert connect(server, option) == ("Hey Computer", 0)


def test_a_switch_with_the_wake_word_off_reads_once_and_stays_off():
    server = SimpleNamespace(wake_word_enabled=False, wake_word_told=None,
                             ha_select_stale=False)
    assert connect(server, "Hey Jarvis") == (NO_WAKE_WORD, 0)
    assert connect(server, NO_WAKE_WORD) == (NO_WAKE_WORD, 0)
