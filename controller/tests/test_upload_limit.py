"""
The firmware upload must not be capped below a firmware.

aiohttp's `client_max_size` defaults to 1 MB and `create_app` never set it,
so /api/releases/upload rejected every real binary — the build is ~10.7 MB.
Both callers post there: the dashboard's Local Build panel and
controller/tools/ota.py. The failure arrived as a 500 reading "An internal
error occurred", because the transport raises before the handler runs and the
handler's own `except Exception` turned aiohttp's 413 into a 500.

A REGRESSION, NOT AN OLD BUG. Measured across the two pinned versions with a
3 MB multipart POST at a default Application:

    aiohttp 3.13.5  -> 200   (streaming multipart bypassed the limit)
    aiohttp 3.14.3  -> 413   HTTPRequestEntityTooLarge

3.13.5 held from May until 2026-08-18, when #129's routine half moved to
3.14.3. Local deploys worked for the life of the project until that day.

Nothing caught it for three reasons worth keeping in mind. The handler DOES
carry a 50 MB check, so reading the code suggests large uploads were
considered — that check sits after the transport limit and never ran. The
ordinary release path never touches this endpoint, because em_firmware
fetches the binary controller-side and pushes from there, so only a developer
deploying a local build meets it. And a dependency bump changed BEHAVIOUR
rather than an interface, which no import, no type and no green check can
see — the same class of gap as #224 (numpy/onnxruntime bumps needing a wake
word comparison behind them).

em_api imports aiohttp and the whole controller stack, so these assert on the
shipped source in the established style (see test_update_interval.py).
"""

import re
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _src() -> str:
    return (CONTROLLER / "em_api.py").read_text()


def test_the_app_raises_the_transport_limit_above_a_firmware():
    """
    A default aiohttp Application caps bodies at 1 MB. The firmware is an
    order of magnitude larger, so create_app must set client_max_size or the
    upload endpoint cannot work at all.
    """
    src = _src()
    m = re.search(r"web\.Application\((.*?)\)\n", src, re.S)
    assert m, "create_app's web.Application call not found"
    assert "client_max_size" in m.group(1), (
        "web.Application must set client_max_size — the 1 MB default is "
        "below the ~10.7 MB firmware and rejects every upload"
    )


def test_the_transport_limit_is_looser_than_the_handler_limit():
    """
    The handler's 413 names the real ceiling. It can only ever be the error a
    user sees if the transport lets the body arrive first — set them equal and
    the useful message becomes unreachable, with aiohttp's terse version
    winning every time.
    """
    src = _src()
    assert "UPLOAD_MAX_BYTES" in src, "the limit should be a named constant"

    m = re.search(r"client_max_size\s*=\s*([^,\n]+)", src)
    assert m, "client_max_size assignment not found"
    expr = m.group(1)
    assert "UPLOAD_MAX_BYTES" in expr and "+" in expr, (
        "the transport limit must be derived from UPLOAD_MAX_BYTES plus "
        f"headroom so the handler's message wins; found: {expr!r}"
    )


def test_the_handler_does_not_swallow_aiohttp_http_exceptions():
    """
    HTTPRequestEntityTooLarge is raised by the transport, not by us. The
    upload handler's blanket `except Exception` caught it and returned a 500
    with no size in the message, which is how a size limit presented as an
    internal error. The middleware re-raises HTTPException deliberately; the
    handler has to as well, and BEFORE the general clause.
    """
    src = _src()
    start = src.index("async def _post_upload_binary")
    body = src[start:start + 4000]

    assert "except web.HTTPException" in body, (
        "the upload handler must re-raise aiohttp's own HTTP exceptions "
        "rather than turning a 413 into a 500"
    )
    assert body.index("except web.HTTPException") < body.index("except Exception"), (
        "the HTTPException clause must come first, or the general clause "
        "catches it and the fix does nothing"
    )
