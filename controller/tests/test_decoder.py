"""
em_decoder — decoder/md5-tool detection for _stream_file_to_device.

Split out and tested for the same reason em_linkauth/em_shadow are: this had
zero coverage before (em_api.py needs aiohttp, deliberately excluded from the
pure-logic suite), and it has real history — crown failed every provisioning
transfer with "no base64 decoder" until a plain `base64` branch was added
between busybox and python3/python (journal, 2026-08-26). A test pins the
order and the two decode.
"""

import em_decoder as D


def _buf(*lines: str) -> str:
    return "\n".join(lines) + "\n__DETECT_DONE__\n"


def test_busybox_preferred_when_present():
    d = D.decide_decoder(_buf("DECODER:busybox", "MD5:busybox"), "__DETECT_DONE__")
    assert d.decode_cmd == "busybox base64 -d"
    assert d.failure is None


def test_plain_base64_used_when_no_busybox():
    # The exact crown regression: no busybox, real base64 in PATH.
    d = D.decide_decoder(_buf("DECODER:plain", "MD5:plain"), "__DETECT_DONE__")
    assert d.decode_cmd == "base64 -d"
    assert d.failure is None


def test_python3_fallback():
    d = D.decide_decoder(_buf("DECODER:python3", "MD5:none"), "__DETECT_DONE__")
    assert d.decode_cmd is not None
    assert "python3" in d.decode_cmd
    assert d.failure is None


def test_python_last_resort():
    d = D.decide_decoder(_buf("DECODER:python", "MD5:none"), "__DETECT_DONE__")
    assert d.decode_cmd is not None
    assert d.decode_cmd.startswith("python ")
    assert d.failure is None


def test_device_genuinely_has_no_decoder_is_not_a_link_problem():
    """
    The marker arrived (device answered), but every decoder check failed —
    this is a property of the device, not worth retrying.
    """
    d = D.decide_decoder(_buf("DECODER:none", "MD5:none"), "__DETECT_DONE__")
    assert d.decode_cmd is None
    assert d.failure == "decoder"


def test_no_output_at_all_is_a_link_problem_not_a_missing_decoder():
    """
    #121's actual bug: conflating "no output within the timeout" (link) with
    "device answered none" (decoder) sent people chasing the wrong half.
    """
    d = D.decide_decoder("", "__DETECT_DONE__")
    assert d.decode_cmd is None
    assert d.failure == "link"


def test_partial_garbage_output_with_no_marker_is_still_a_link_problem():
    d = D.decide_decoder("some partial junk with no marker", "__DETECT_DONE__")
    assert d.failure == "link"


def test_decoder_order_busybox_beats_everything_else_present():
    # A device could in principle answer multiple positives if the probe
    # script were ever changed carelessly — busybox must still win, matching
    # the real if/elif order in em_api.py.
    d = D.decide_decoder(
        _buf("DECODER:busybox", "DECODER:plain", "DECODER:python3"),
        "__DETECT_DONE__")
    assert d.decode_cmd == "busybox base64 -d"


def test_md5_busybox_preferred():
    assert D.decide_md5(_buf("MD5:busybox")) == "busybox md5sum"


def test_md5_plain_fallback():
    assert D.decide_md5(_buf("MD5:plain")) == "md5sum"


def test_md5_none_when_absent():
    assert D.decide_md5(_buf("MD5:none")) is None
    assert D.decide_md5("") is None
