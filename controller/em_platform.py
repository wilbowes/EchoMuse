"""
Which userspace a device booted on, and what follows from it.

The firmware runs on two bases and will for as long as the fleet runs FireOS:
Amazon's Android 5.1 userspace, and emOS, which replaces it with our own init
on the same kernel. The device reports which one it booted as `baseOs` on the
stats message (device/internal/platform), and this module holds the decisions
the controller makes off the back of it.

Pure and dependency-free, for the reason `em_linkauth.decide` and
`em_button.decide` are: the rule below is one comparison and getting it
backwards is silent, so it belongs somewhere a test can reach without
aiohttp.
"""

# The three answers the device can give. Mirrored from
# device/internal/platform/platform.go and pinned by test — a typo here makes
# every device look like Android forever, which is exactly the failure the
# capability-string mirroring exists to prevent.
EMOS = "emos"
FIREOS = "fireos"
UNKNOWN = "unknown"

# The wire key on the REGISTER message. Snake case, matching its neighbours
# there (`ambient_light_status`, `firmware_ver`) rather than the camelCase of
# the stats report it briefly rode on.
REGISTER_KEY = "base_os"


def android_userspace(base_os) -> bool:
    """
    Whether Android-only payloads mean anything on this device.

    True for everything except a device that has POSITIVELY said `emos`.
    Firmware too old to report the field, a device that has not sent stats
    yet, and any value we do not recognise all keep today's behaviour.

    That asymmetry is deliberate and is the whole content of this function.
    The project's rule is to degrade to old behaviour rather than to a wrong
    answer, and the two ways of being wrong here are not equal:

    - Treating an Android device as emOS skips the debloat, leaving a device
      slightly fatter than intended. Recoverable, invisible, cheap.
    - Treating an emOS device as Android runs `pm hide` against a system with
      no package manager and writes a Magisk service.d script no init will
      ever read. It wastes a shell round trip per reconnect and reports
      success at having done nothing.

    So absence resolves toward Android, which is where every device was
    before emOS existed.
    """
    return base_os != EMOS


def label(base_os) -> str:
    """
    How to name the base in something a person reads.

    None becomes "unknown" rather than an empty string or "FireOS": a device
    whose firmware predates the field has not told us it is on Android, and a
    panel that says FireOS anyway is stating a fact nobody established.
    """
    if base_os == EMOS:
        return "emOS"
    if base_os == FIREOS:
        return "FireOS"
    return "unknown"
