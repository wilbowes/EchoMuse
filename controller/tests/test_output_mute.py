import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from em_output_mute import OutputMute


def test_mute_then_unmute_restores_the_level():
    m = OutputMute()
    assert m.mute(96) == 0
    assert m.muted
    assert m.unmute() == 96
    assert not m.muted


def test_mute_and_unmute_are_idempotent():
    m = OutputMute()
    assert m.unmute() is None
    m.mute(80)
    assert m.mute(10) is None, "a second mute must not replace the level to restore"
    assert m.unmute() == 80
    assert m.unmute() is None


def test_our_own_zero_is_not_a_volume_to_keep():
    # The device echoes volume_set(0) back as volume_state 0; persisting it
    # would make 0 the startup volume and lose the level unmute restores.
    m = OutputMute()
    m.mute(90)
    assert m.device_report(0) == (False, None)
    assert m.muted and m.restore_level == 90


def test_volume_up_on_the_echo_unmutes_one_step_above_the_old_level():
    # UAT 2026-09-27: the device stepped up from 0 to its button floor (47)
    # and that replaced the stored volume. It should come back above 102.
    m = OutputMute()
    m.mute(102)
    assert m.device_report(47) == (False, 110)
    assert not m.muted
    assert m.unmute() is None


def test_volume_up_restore_is_capped_at_unity():
    m = OutputMute()
    m.mute(124)
    assert m.device_report(47) == (False, 127)


def test_a_volume_from_ha_unmutes():
    m = OutputMute()
    m.mute(90)
    m.volume_set(60)
    assert not m.muted
    assert m.unmute() is None


def test_reports_while_unmuted_are_always_kept():
    m = OutputMute()
    assert m.device_report(0) == (True, None), "volume 0 set on purpose is a real volume"
    assert m.device_report(85) == (True, None)


def test_a_reconnect_reapplies_the_mute():
    m = OutputMute()
    assert m.on_reconnect() is None
    m.mute(70)
    assert m.on_reconnect() == 0
    m.unmute()
    assert m.on_reconnect() is None


def test_mute_is_handled_where_it_is_advertised():
    # We advertised VOLUME_MUTE and dropped the command for months (#641).
    import re
    src = open(os.path.join(os.path.dirname(__file__), "..", "em_esphome.py")).read()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "MediaPlayerEntityFeature.VOLUME_MUTE" in code
    assert re.search(r"self\._apply_output_mute\(cmd == api_pb2\.MEDIA_PLAYER_COMMAND_MUTE\)", code)
    assert not re.search(r"muted\s*=\s*False", code), \
        "the entity must report the real mute, never a constant"
