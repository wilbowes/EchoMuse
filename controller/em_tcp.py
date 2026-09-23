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
