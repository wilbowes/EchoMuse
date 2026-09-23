"""Thin-stream TCP on the controller's end of the device link.

The link loses packets (#139: 4.6-7.1%; far more while a Dot's BLE scan runs,
2026-09-23), and TCP turns each loss into a wait that doubles with every
further loss. For a connection with fewer than four segments in flight - the
control plane nearly always - Linux's thin-stream option retransmits on a
LINEAR timer for the first six retries instead. The retransmits sent from this
end are the pings, LED and turn commands, and the start of every reply.

HA OS already sets net.ipv4.tcp_thin_linear_timeouts=1 system-wide; a plain
Docker host defaults to 0, so the container sets it per socket and the two
deployments behave alike. The device sets the same on its own sockets
(device/internal/client/tcptune.go).
"""

import logging
import socket
import struct

log = logging.getLogger("echomuse.tcp")

# Numbers from include/uapi/linux/tcp.h, for a Python that lacks the names.
TCP_THIN_LINEAR_TIMEOUTS = getattr(socket, "TCP_THIN_LINEAR_TIMEOUTS", 16)

_warned = False


def tune(sock) -> bool:
    """Set thin-stream retransmission on an accepted device socket.

    Never raises: an untuned socket is the old behaviour, not a broken one.
    Returns whether the option was applied.
    """
    global _warned
    if sock is None:
        return False
    try:
        sock.setsockopt(socket.IPPROTO_TCP, TCP_THIN_LINEAR_TIMEOUTS, 1)
        return True
    except OSError as e:
        if not _warned:
            _warned = True
            log.warning(f"Thin-stream TCP not applied ({e}) - device link keeps exponential backoff")
        return False


# ─── Link health from the kernel's own counters ──────────────────────────────
#
# The loss above was invisible for months because nothing reported it: the MTK
# driver's RF counters are structurally zero, ICMP to a Dot is dropped, and the
# app-level RTT shows the symptom without the cause. TCP knows exactly how many
# segments it had to send twice. Reading that off the controller's own socket
# to each device gives DOWNLINK loss - the direction the AP had to resend on -
# once per stats report, for the cost of one getsockopt per plane.

# struct tcp_info (include/uapi/linux/tcp.h). Append-only across kernels, so
# fixed offsets are safe; fields past what a kernel fills read as zero.
_TCP_INFO_LEN = 144
_OFF_RTO_US = 8
_OFF_TOTAL_RETRANS = 100
_OFF_SEGS_OUT = 136


def read_info(sock):
    """(segs_out, total_retrans, rto_ms) for a TCP socket, or None.

    None when the socket is gone or the platform has no TCP_INFO, which the
    caller records as "not measured" - never as a clean link.
    """
    if sock is None or not hasattr(socket, "TCP_INFO"):
        return None
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, _TCP_INFO_LEN)
    except OSError:
        return None
    if len(raw) < _OFF_SEGS_OUT + 4:
        return None
    rto_us, = struct.unpack_from("<I", raw, _OFF_RTO_US)
    retrans, = struct.unpack_from("<I", raw, _OFF_TOTAL_RETRANS)
    segs, = struct.unpack_from("<I", raw, _OFF_SEGS_OUT)
    return segs, retrans, rto_us // 1000


class LossWindow:
    """Turns cumulative per-socket counters into per-report deltas.

    Counters are per CONNECTION and start again at zero on a reconnect, so a
    snapshot is keyed by the socket object; a key not seen last time (or one
    whose counters went backwards) contributes nothing until it has a baseline
    - a reconnect must not read as a burst of millions of segments.
    """

    def __init__(self):
        self._last: dict = {}

    def drain(self, snapshots: dict) -> dict:
        """snapshots: {key: (segs_out, total_retrans, rto_ms) | None}.

        Returns tcpDownSegs / tcpDownRetrans summed over the sockets with a
        baseline, and tcpRtoMaxMs over every socket read. Empty when nothing
        could be read, so absence stores as NULL.
        """
        segs = retrans = 0
        measured = False
        rto_max = None
        now: dict = {}
        for key, snap in snapshots.items():
            if snap is None:
                continue
            s, r, rto = snap
            now[key] = (s, r)
            rto_max = rto if rto_max is None else max(rto_max, rto)
            prev = self._last.get(key)
            if prev is None or s < prev[0] or r < prev[1]:
                continue
            segs += s - prev[0]
            retrans += r - prev[1]
            measured = True
        self._last = now
        out = {}
        if measured:
            out["tcpDownSegs"] = segs
            out["tcpDownRetrans"] = retrans
        if rto_max is not None:
            out["tcpRtoMaxMs"] = rto_max
        return out
