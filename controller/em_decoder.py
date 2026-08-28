"""
Decoder/md5-tool detection for `_stream_file_to_device` (em_api.py), split out
as pure functions for the reason `em_linkauth.decide`/`em_shadow.decide_wake_source`
already are: the decision itself has no coverage at all otherwise, since
em_api.py needs aiohttp/websockets and is deliberately excluded from the
pure-logic test suite (controller/CLAUDE.md).

This logic has real history worth protecting with a test rather than trusting
by eye: crown (LineageOS toybox userland, no busybox) failed every
secure_link/provisioning transfer with "no base64 decoder" until a plain
`base64` branch was added between busybox and python3/python detection
(journal, 2026-08-26) — the exact kind of one-word regression a hand-verified
if/elif chain won't reliably catch on a later edit.
"""

from dataclasses import dataclass


@dataclass
class DecoderDecision:
    # Populated on success; both None on failure.
    decode_cmd: str | None
    # One of "link", "decoder", or None (success).
    failure: str | None
    # Only meaningful on failure — distinguishes "the device answered and
    # genuinely has nothing" (not worth retrying) from "the shell produced no
    # output at all" (a link problem, worth retrying) — conflating these sent
    # #121 looking at the wrong half of the problem.
    detail: str = ""


def decide_decoder(detect_buf: str, detect_marker: str) -> DecoderDecision:
    """
    Given the raw text collected from the detection round-trip, decide which
    decoder command to use, or why none is usable.

    Order matters and mirrors the actual probe order in em_api.py: busybox
    first (Magisk provides it), then plain `base64` in PATH (toybox on
    stock/LineageOS — crown has this and no busybox), then python3/python as
    a last resort.
    """
    if "DECODER:busybox" in detect_buf:
        return DecoderDecision("busybox base64 -d", None)
    if "DECODER:plain" in detect_buf:
        return DecoderDecision("base64 -d", None)
    if "DECODER:python3" in detect_buf:
        return DecoderDecision(
            "python3 -c 'import sys,base64; "
            "sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))'",
            None)
    if "DECODER:python" in detect_buf:
        return DecoderDecision(
            "python -c 'import sys,base64; "
            "sys.stdout.write(base64.b64decode(sys.stdin.read()))'",
            None)

    # Two very different things reach here. The marker present means the
    # device answered and genuinely has no decoder — a property of that
    # device, which retrying will not change. Absent means the round trip
    # produced nothing at all, i.e. the shell plane is not carrying output,
    # which IS worth retrying.
    if detect_marker not in detect_buf:
        return DecoderDecision(None, "link", detect_buf)
    return DecoderDecision(None, "decoder", detect_buf)


def decide_md5(detect_buf: str) -> str | None:
    """Which md5 tool to use for verification, or None if there isn't one."""
    if "MD5:busybox" in detect_buf:
        return "busybox md5sum"
    if "MD5:plain" in detect_buf:
        return "md5sum"
    return None
