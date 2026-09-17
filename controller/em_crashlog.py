"""
Read an emOS device's saved kernel log and decide whether its last boot crashed.

emOS init copies MediaTek's ram console (/proc/last_kmsg) to
/data/emos/last_kmsg.prev on every boot, rotating older copies. It is the only
crash channel this device has — no pstore, and a kernel crash leaves nothing in
any userspace log — but until 2026-09-17 nothing read it, so a crash was only
found if someone opened a USB console before the next reboot overwrote the ram
console.

Pure, so the classification is testable without a device.

**A clean end is positive evidence; a crash is its absence.** Every orderly
restart logs "reboot: Restarting system" (or "reboot: Power down"), and MediaTek
dumps a `Call trace:` on EVERY restart, so a stack trace means nothing by itself.
A crash does not reliably leave a panic line either: the one caught on C95
(2026-09-04) was a bus read timeout in the audio IRQ handler whose own printk
faulted again, recursing until the hardware reset. So a boot is "clean" only if
the restart line is present, and the crash markers below only choose where the
excerpt is taken from.
"""
import re

from em_support import _IPV4, _MAC

# /data/emos/last_kmsg.prev and the marker recording which copy was collected.
KMSG_PATH = "/data/emos/last_kmsg.prev"
SEEN_PATH = "/data/emos/last_kmsg.prev.seen"

_CLEAN_END = re.compile(r"reboot: (Restarting system|Power down)")

# Lines that start a failure. The first match anchors the excerpt.
_CRASH_MARKERS = re.compile(
    r"Kernel panic|Internal error:|Unable to handle kernel|Oops|BUG:|"
    r"do_mem_abort|el1_da|read_timeout_handler|Unhandled fault|"
    r"hang_detect|HWT|WDT FIQ|wdt_fiq|Watchdog detected"
)

# The WLAN driver logs the network name on association.
_SSID = re.compile(r"ssid", re.I)

_UPTIME = re.compile(r"^\[\s*(\d+)\.\d+\]")

EXCERPT_BEFORE = 30
EXCERPT_AFTER = 60
TAIL_LINES = 40
MAX_CHARS = 8000


def is_clean(text: str) -> bool:
    """True if the log records an orderly restart or power-down."""
    return bool(_CLEAN_END.search(text))


def redact(lines: list[str]) -> list[str]:
    """Drop lines naming a network and mask addresses. The excerpt is stored
    as a device log event, which reaches support bundles."""
    out = []
    for ln in lines:
        if _SSID.search(ln):
            continue
        ln = _IPV4.sub("<ip>", ln)
        ln = _MAC.sub("<mac>", ln)
        out.append(ln)
    return out


def last_uptime(lines: list[str]) -> int | None:
    """Seconds since boot on the last timestamped line: how long the device ran."""
    for ln in reversed(lines):
        m = _UPTIME.match(ln)
        if m:
            return int(m.group(1))
    return None


def summarise(text: str) -> str | None:
    """
    None for a clean boot. Otherwise a log message: how long the boot ran, and
    an excerpt around the first crash marker, or the tail if there is none —
    a watchdog reset or a hang can end the log with nothing to anchor on.
    """
    if is_clean(text):
        return None
    lines = [ln.rstrip() for ln in text.splitlines()]
    first = next((i for i, ln in enumerate(lines) if _CRASH_MARKERS.search(ln)), None)
    if first is None:
        excerpt = lines[-TAIL_LINES:]
        where = f"no crash marker found; last {len(excerpt)} lines"
    else:
        lo = max(0, first - EXCERPT_BEFORE)
        excerpt = lines[lo:first + EXCERPT_AFTER]
        where = f"from line {lo + 1}, around the first crash marker"
    excerpt = redact(excerpt)
    body = "\n".join(excerpt)
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "\n…(truncated)"
    up = last_uptime(lines)
    ran = f" after {up}s" if up is not None else ""
    return (f"Previous boot ended without a clean restart{ran} — kernel log "
            f"({where}):\n{body}")
