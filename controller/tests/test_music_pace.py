import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from em_music_pace import MusicPace, REPORT_FRESH_S

RATE = 96000.0  # mono S16 at 48kHz


def test_no_report_means_no_estimate():
    p = MusicPace()
    p.on_send("ws", 4096)
    assert p.buffered_s(0.0, RATE) is None


def test_buffer_is_lead_plus_in_transit():
    p = MusicPace()
    p.on_send("ws", 5 * 96000)
    # Device holds 2s and has received 4s of the 5s sent: 1s in transit.
    assert p.on_report("ws", 2000, 4 * 96000, now=10.0)
    assert abs(p.buffered_s(10.0, RATE) - 3.0) < 1e-9


def test_estimate_drains_at_playback_rate():
    p = MusicPace()
    p.on_send("ws", 96000)
    p.on_report("ws", 3000, 96000, now=0.0)
    assert abs(p.buffered_s(1.5, RATE) - 1.5) < 1e-9


def test_sends_after_the_report_count():
    p = MusicPace()
    p.on_send("ws", 96000)
    p.on_report("ws", 1000, 96000, now=0.0)
    p.on_send("ws", 48000)
    assert abs(p.buffered_s(0.0, RATE) - 1.5) < 1e-9


def test_a_new_connection_forgets_the_old_one():
    # The 2026-09-27 case: bytes written to a connection that dropped must not
    # count as delivered, or the feed holds back and the buffer never refills.
    p = MusicPace()
    p.on_send("old", 10 * 96000)
    p.on_report("old", 4000, 9 * 96000, now=0.0)
    p.on_send("new", 96000)
    assert p.buffered_s(0.0, RATE) is None
    assert p.on_report("new", 500, 96000, now=1.0)
    assert abs(p.buffered_s(1.0, RATE) - 0.5) < 1e-9


def test_a_report_about_the_old_connection_is_refused():
    p = MusicPace()
    p.on_send("new", 96000)
    # Generated before the reconnect: claims more than the new one carried.
    assert not p.on_report("new", 4000, 9 * 96000, now=0.0)
    assert not p.on_report("other", 1000, 0, now=0.0)
    assert not p.on_report(None, 1000, 0, now=0.0)
    assert p.buffered_s(0.0, RATE) is None


def test_stale_report_falls_back():
    p = MusicPace()
    p.on_send("ws", 96000)
    p.on_report("ws", 3000, 96000, now=0.0)
    assert p.buffered_s(REPORT_FRESH_S + 0.1, RATE) is None


def test_malformed_report_is_refused():
    p = MusicPace()
    p.on_send("ws", 96000)
    assert not p.on_report("ws", "x", 0, now=0.0)
    assert not p.on_report("ws", 1000, None, now=0.0)
    assert not p.on_report("ws", 1000, -1, now=0.0)


ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def test_both_halves_negotiate_the_report():
    # The device sends reports only to a controller announcing the feature,
    # and the controller must announce it for them to be used at all.
    import re
    ctl = _read("controller", "em_controller.py")
    feats = re.search(r"^CONTROLLER_FEATURES = \[(.*?)\]", ctl, re.S | re.M)
    assert feats and '"music_buffer"' in feats.group(1)
    assert re.search(r'^const FeatureMusicBuffer = "music_buffer"$',
                     _read("device", "internal", "client", "control.go"), re.M)
    assert re.search(r"if controlClient\.HasFeature\(client\.FeatureMusicBuffer\) && ",
                     _read("device", "cmd", "server.go"))
