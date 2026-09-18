"""em_labels: the 32-character device label cap, and its dashboard mirror."""
import re
from pathlib import Path

import em_labels

CONTROLLER = Path(__file__).resolve().parents[1]


def test_a_label_up_to_the_cap_is_accepted():
    label, err = em_labels.check_label("x" * em_labels.MAX_LABEL_LEN)
    assert err is None and len(label) == em_labels.MAX_LABEL_LEN


def test_one_over_the_cap_is_refused_and_says_why():
    label, err = em_labels.check_label("x" * (em_labels.MAX_LABEL_LEN + 1))
    assert label is None and "33" in err and "32" in err


def test_surrounding_whitespace_does_not_count():
    label, err = em_labels.check_label("  " + "x" * 32 + "  \n")
    assert err is None and label == "x" * 32


def test_empty_or_missing_is_refused():
    for value in ("", "   ", None, 7):
        label, err = em_labels.check_label(value)
        assert label is None and err


def test_the_dashboard_mirrors_the_cap():
    jsx = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    m = re.search(r"const _MAX_LABEL = (\d+);", jsx)
    assert m, "dashboard.jsx must define _MAX_LABEL"
    assert int(m.group(1)) == em_labels.MAX_LABEL_LEN


def test_both_label_endpoints_enforce_it():
    api = (CONTROLLER / "em_api.py").read_text()
    for handler in ("async def _patch_device", "async def _post_approve"):
        body = api[api.index(handler):]
        body = body[:body.index("\n@", 1)]
        assert "_require_label(body)" in body, f"{handler} must use _require_label"
