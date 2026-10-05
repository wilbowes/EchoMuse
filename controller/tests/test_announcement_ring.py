"""
The ring during an announcement (#779): a Home Assistant announcement, and
the opening message of a conversation it starts, played with the ring dark
while every other spoken reply showed the playback meter.
"""

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import em_scenes

CONTROLLER = Path(__file__).resolve().parent.parent


def test_an_announcement_shows_the_ring():
    assert em_scenes.announcement_ring(
        capable=True, turn_running=False, alarm_ringing=False)


def test_it_leaves_the_ring_to_a_turn_or_a_ringing_timer():
    """Taking the ring would also mean clearing it under them afterwards."""
    assert not em_scenes.announcement_ring(
        capable=True, turn_running=True, alarm_ringing=False)
    assert not em_scenes.announcement_ring(
        capable=True, turn_running=False, alarm_ringing=True)


def test_firmware_without_ring_animations_is_left_alone():
    assert not em_scenes.announcement_ring(
        capable=False, turn_running=False, alarm_ringing=False)


def test_the_announcement_path_raises_the_meter_and_clears_it():
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    play = ast.unparse(next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_standalone_play"))
    raised = play.index("send_led_anim(meter)")
    played = play.index("_run_post_turn_playback(")
    cleared = play.index("leds_off(")
    assert raised < played < cleared
    assert "em_scenes.announcement_ring(" in play
