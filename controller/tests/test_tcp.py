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
