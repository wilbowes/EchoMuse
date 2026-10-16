"""
The speech gate: a turn's audio reaches HA only once a frame scores as speech,
and when it does, HA receives the whole held stream, in order.

Silero itself needs onnxruntime, which the suite does not have, so the model
is stubbed; its per-turn state handling is tested against a stub session.
"""

import numpy as np

import em_speechgate as sg


def _frames(n):
    return [bytes([i]) * 2560 for i in range(n)]


def test_holds_everything_below_threshold():
    g = sg.SpeechGate(threshold=0.5)
    for f in _frames(20):
        assert g.push(f, 0.03) == []
    assert not g.open
    assert g.peak == 0.03


def test_opening_releases_the_held_stream_in_order_then_passes_through():
    g = sg.SpeechGate(threshold=0.5)
    fs = _frames(6)
    for f in fs[:3]:
        g.push(f, 0.1)
    # The opening frame comes out LAST, after what was held before it: HA must
    # receive exactly the ungated stream, only later.
    assert g.push(fs[3], 0.9) == fs[:4]
    assert g.open and g.opened_at == 3
    # Once open, nothing is scored against the bar: speech has pauses.
    assert g.push(fs[4], 0.0) == [fs[4]]
    assert g.push(fs[5], 0.0) == [fs[5]]


def test_threshold_is_inclusive():
    g = sg.SpeechGate(threshold=0.5)
    assert g.push(b"x", 0.5) == [b"x"]


def test_hold_is_bounded_and_keeps_the_newest():
    g = sg.SpeechGate(threshold=0.5, max_hold_s=0.4)   # 5 frames
    fs = _frames(8)
    for f in fs[:7]:
        g.push(f, 0.0)
    assert g.dropped == 2
    assert g.push(fs[7], 1.0) == fs[3:8]


def test_summary_says_nothing_was_sent_when_it_never_opened():
    g = sg.SpeechGate()
    for f in _frames(3):
        g.push(f, 0.02)
    assert "nothing sent" in g.summary()
    assert "peak 0.02" in g.summary()


class _Session:
    """Stub ONNX session: probability = mean |x| of the chunk; counts state."""

    def __init__(self):
        self.calls = []

    def run(self, _outs, feeds):
        x = feeds["input"]
        assert x.shape == (1, sg.CHUNK)
        self.calls.append(float(feeds["h"][0, 0, 0]))
        h = feeds["h"] + 1.0     # state must be carried from call to call
        return [np.array([[float(np.abs(x).mean())]]), h, feeds["c"]]


def test_silero_scores_two_chunks_per_frame_and_carries_state():
    sess = _Session()
    v = sg.Silero(sess)
    loud = (np.ones(1280) * 16384).astype(np.int16).tobytes()
    p = v.prob(loud)
    assert abs(p - 0.5) < 1e-3
    v.prob(loud)
    assert sess.calls == [0.0, 1.0, 2.0, 3.0]


def test_threshold_clamps_and_a_bad_value_keeps_the_default():
    assert sg.clamp_threshold(0.4) == 0.4
    assert sg.clamp_threshold(0.05) == sg.GATE_LO
    assert sg.clamp_threshold(9) == sg.GATE_HI
    assert sg.clamp_threshold("nope") == sg.THRESHOLD
    assert sg.clamp_threshold(float("nan")) == sg.THRESHOLD


def test_apply_and_threshold_for_use_the_device_score():
    class Dev:
        pass
    dev = Dev()
    sg.apply(dev, {"speechGateThreshold": 0.33})
    assert sg.threshold_for(dev) == 0.33
    sg.apply(dev, {"speechGateThreshold": 9})
    assert dev.speech_gate_threshold == sg.GATE_HI
    assert sg.threshold_for(object()) == sg.THRESHOLD


def test_addon_env_overlays_the_fleet_score(monkeypatch, tmp_path):
    import em_db
    stored = {"speechGateThreshold": 0.4, "vadThreshold": 0.001}
    monkeypatch.delenv("EM_SPEECH_GATE_THRESHOLD", raising=False)
    assert sg.overlay(stored)["speechGateThreshold"] == 0.4
    assert sg.overlay(stored, {"EM_SPEECH_GATE_THRESHOLD": "0.7"})["speechGateThreshold"] == 0.7
    assert sg.overlay(stored, {"EM_SPEECH_GATE_THRESHOLD": "nope"})["speechGateThreshold"] == 0.5
    em_db.init(str(tmp_path / "em.db"))
    try:
        monkeypatch.setenv("EM_SPEECH_GATE_THRESHOLD", "0.6")
        assert em_db.get_global_device_config()["speechGateThreshold"] == 0.6
    finally:
        em_db._conn.close()
        em_db._conn = None


def test_the_gate_is_built_from_the_device_score():
    import _sources
    body = _sources.def_source("em_esphome._stream_mic_audio")
    assert "em_speechgate.threshold_for(device)" in body
    reg = _sources.def_source("handle_control")
    live = _sources.def_source("apply_live_config")
    assert "em_speechgate.apply(device, config)" in reg
    assert "em_speechgate.apply(live, effective)" in live


def test_a_new_turn_starts_from_clean_state():
    sess = _Session()
    sg.Silero(sess).prob(bytes(2560))
    sess.calls.clear()
    sg.Silero(sess).prob(bytes(2560))
    assert sess.calls[0] == 0.0
