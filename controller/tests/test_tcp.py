"""em_tcp: thin-stream TCP on accepted device sockets.

Read back from a real socket, because what matters is that the kernel holds
the option on the connection the device link uses, not that a call was made.
"""

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import em_tcp  # noqa: E402


@pytest.fixture
def accepted():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname())
    conn, _ = srv.accept()
    yield conn
    for s in (conn, cli, srv):
        s.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux socket option")
def test_tune_sets_thin_linear_timeouts(accepted):
    assert em_tcp.tune(accepted) is True
    got = accepted.getsockopt(socket.IPPROTO_TCP, em_tcp.TCP_THIN_LINEAR_TIMEOUTS)
    assert got == 1


def test_tune_never_raises():
    # An untuned link is the old behaviour, not a broken one: a missing
    # socket or a closed one must not fail the connection.
    assert em_tcp.tune(None) is False
    s = socket.socket()
    s.close()
    assert em_tcp.tune(s) is False


def test_every_device_plane_is_tuned():
    # All three planes (/control, /data, /shell) arrive through _route, so
    # that is where the call must sit, ahead of the dispatch.
    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()
    route = src[src.index("async def _route("):]
    route = route[:route.index("\nasync def ", 1)]
    assert "em_tcp.tune(" in route
    assert route.index("em_tcp.tune(") < route.index("handle_control(")


# ─── LossWindow: cumulative kernel counters → per-report deltas ─────────────

def test_window_needs_a_baseline_before_it_reports():
    w = em_tcp.LossWindow()
    # First sight of a socket is a baseline, not a burst of everything it has
    # ever sent — only the RTO can be reported yet.
    assert w.drain({"ctl": (5000, 40, 204)}) == {"tcpRtoMaxMs": 204}
    out = w.drain({"ctl": (6000, 90, 812)})
    assert out == {"tcpDownSegs": 1000, "tcpDownRetrans": 50, "tcpRtoMaxMs": 812}


def test_planes_sum_and_a_reconnect_starts_again():
    w = em_tcp.LossWindow()
    w.drain({"ctl": (100, 0, 200), "data": (1000, 10, 200)})
    # data reconnected (new key); control carried on.
    out = w.drain({"ctl": (200, 5, 200), "data2": (50, 0, 200)})
    assert out["tcpDownSegs"] == 100 and out["tcpDownRetrans"] == 5
    out = w.drain({"ctl": (300, 5, 200), "data2": (150, 2, 200)})
    assert out["tcpDownSegs"] == 200 and out["tcpDownRetrans"] == 2


def test_counters_going_backwards_are_a_new_connection_not_negative_loss():
    w = em_tcp.LossWindow()
    w.drain({"k": (1000, 100, 200)})
    assert "tcpDownSegs" not in w.drain({"k": (10, 0, 200)})


def test_nothing_readable_reports_nothing():
    w = em_tcp.LossWindow()
    assert w.drain({}) == {}
    assert w.drain({"ctl": None}) == {}


@pytest.mark.skipif(sys.platform != "linux", reason="Linux TCP_INFO")
def test_read_info_reads_a_real_socket(accepted):
    accepted.sendall(b"x" * 10)
    info = em_tcp.read_info(accepted)
    assert info is not None
    segs, retrans, rto_ms = info
    assert segs >= 1 and retrans == 0 and rto_ms >= 200



# ─── MinuteStrip: loss per minute for the Status tab ────────────────────────

def test_strip_buckets_by_minute_and_grades_loss():
    st = em_tcp.MinuteStrip(keep_minutes=5)
    t0 = 60 * 1000                      # a minute boundary
    st.add(t0 + 5, 1000, 0)             # minute 0: clean
    st.add(t0 + 35, 1000, 0)
    st.add(t0 + 65, 1000, 30)           # minute 1: 3% -> fair
    st.add(t0 + 185, 1000, 80)          # minute 3: 8% -> poor; minute 2 missing
    assert st.minutes(t0 + 190) == [None, 0.0, 3.0, None, 8.0]
    s = st.summary(t0 + 190)
    assert s["verdict"] == "fair" and s["lossPct"] == 2.8   # 110 of 4000 over 10 min


def test_strip_forgets_old_minutes_and_unmeasured_is_none():
    st = em_tcp.MinuteStrip(keep_minutes=3)
    assert st.summary(1000.0) is None
    st.add(0, 100, 0)
    st.add(600, None, None)             # a report with no downlink figure
    assert st.summary(600.0) is None    # minute 0 has aged out
    st.add(600, 0, 0)                   # no traffic: nothing to divide by
    assert st.minutes(600.0)[-1] is None


def test_verdict_thresholds():
    assert em_tcp.verdict(None) is None
    assert em_tcp.verdict(0.0) == "good"
    assert em_tcp.verdict(0.99) == "good"
    assert em_tcp.verdict(1.0) == "fair"
    assert em_tcp.verdict(4.99) == "fair"
    assert em_tcp.verdict(5.0) == "poor"
