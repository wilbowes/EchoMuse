"""
The device-link auth decision, as a pure function.

Split out of `em_controller._link_auth_ok` so it can be tested. The rest of
that function is a websocket header read, a DB lookup and a log call; the part
worth getting right is the four-way decision below, and it was previously
unreachable from the test suite because em_controller pulls in the whole
websockets/openwakeword stack.

It cost an orphaned device to find out. Deleting a device removed its row, and
the token is a column on that row, so `expected` became None while the device
carried on presenting the credential it still had on disk. The rule rejected
that, on all three planes including the shell plane the controller would
otherwise have used to push a fresh credential, and the device retried forever
behind a pulsing orange ring.
"""

import hmac
import ipaddress
from typing import NamedTuple, Optional


# Why a device that has presented its token before was refused without it.
# Shown on the dashboard, so it says what to do.
MISSING_CREDENTIAL = "missing its link credential"


class Verdict(NamedTuple):
    ok: bool
    # Why, for the log. None when there is nothing worth saying.
    reason: Optional[str] = None
    # True when the connection is allowed but carried a credential nothing
    # recognises — worth a log line, since it is almost always a device that
    # was deleted and has come back.
    stale_token: bool = False


def decide(
    *,
    presented: Optional[str],
    expected: Optional[str],
    confirmed: bool,
    secure: bool,
    require_tls: bool,
) -> Verdict:
    """
    Whether to admit a device connection.

    `presented` is the X-EM-Token header, `expected` the stored token for this
    device id, `confirmed` whether this device has ever presented `expected`,
    `secure` whether the connection arrived over TLS.

    Four rules:

    1. A presented token that MISMATCHES a stored one always rejects. This is
       the only case where a credential is actually wrong.

    2. A stored token with NOTHING presented is allowed unless `require_tls`,
       or unless the device has presented that token before (`confirmed`). The
       DB row is minted before the files reach the device, and rejecting in
       that window would cut off the shell plane that the credential push
       itself rides on. Once the device has shown it holds the token, the
       window is over: the device id is public (mDNS carries most of the
       serial), so admitting a missing token let anyone on the LAN be any
       Echo. Recovery for a device that really lost its credential is Remove
       and approve again, via rule 3 (Wil, 2026-09-26).

    3. A token presented for a device with nothing on record is IGNORED, not
       rejected. With nothing on record there is nothing to check it against,
       and a connection presenting no token is admitted as pending too, so
       rejecting it buys nothing; it is what made deleting a device a one-way
       door. Such a device comes back as pending and waits for approval, which
       is a human decision anyway.

    4. `require_tls` demands TLS AND a token matching a stored one, full stop.
       A deleted device is still refused there, and re-provisioning over USB
       stays the intended path.
    """
    if presented and expected and not hmac.compare_digest(presented, expected):
        return Verdict(False, "token mismatch")

    if require_tls and not (secure and presented and expected):
        return Verdict(False,
                       "plain connection" if not secure else "missing a valid token")

    if expected and not presented and confirmed:
        return Verdict(False, MISSING_CREDENTIAL)

    if presented and not expected:
        return Verdict(True, "token presented but none on record", stale_token=True)

    return Verdict(True)


def _ip(addr: Optional[str]):
    """An address as an ip_address, with IPv4-mapped IPv6 unwrapped; None if unparseable."""
    try:
        a = ipaddress.ip_address((addr or "").split("%")[0])
    except ValueError:
        return None
    return a.ipv4_mapped or a if a.version == 6 else a


def follows_control(*, control_peer: Optional[str], control_secure: bool,
                    peer: Optional[str], secure: bool) -> Optional[str]:
    """
    Why a /data or /shell connection must be refused because it does not come
    from the device's live control connection, or None to admit it.

    Token checks alone cannot stop this for a device that has never presented
    a token: such a device is admitted without one, so anyone knowing its
    (public) id could take over its audio on /data, or win the race for
    /shell while a credential push is waiting on it and receive the token.
    A real device dials all three from the same address, with the same scheme.

    An unparseable address on either side admits: behind a proxy that hides
    peers this check has nothing to compare, and refusing would take down
    every device rather than protect one.
    """
    if control_secure and not secure:
        return "plain connection while the control plane is TLS"
    a, b = _ip(control_peer), _ip(peer)
    if a is not None and b is not None and a != b:
        return f"from {b}, not the control plane's {a}"
    return None
