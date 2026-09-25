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


# ── Beyond length: what Home Assistant accepts but should not be given ──────
# Edges from Unicode's own categories rather than from typical names.

def test_every_control_character_is_refused():
    # All of Cc: C0 (U+0000-U+001F), DEL (U+007F), C1 (U+0080-U+009F).
    for cp in [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)]:
        label, err = em_labels.check_label(f"Kit{chr(cp)}chen")
        assert label is None and "control" in err, hex(cp)


def test_a_control_character_at_the_ends_is_trimmed_not_refused():
    # str.strip() removes \t \n \r \x0b \x0c and \x1c-\x1f; what it removes
    # never reaches the check, the same as a pasted trailing newline.
    label, err = em_labels.check_label("\tKitchen\n")
    assert err is None and label == "Kitchen"


def test_a_lone_surrogate_is_refused():
    # json.loads('"\\ud800"') succeeds, and the label is later UTF-8 encoded
    # for the mDNS TXT record, which would raise.
    for s in ("\ud800", "Kitchen\udfff"):
        label, err = em_labels.check_label(s)
        assert label is None and err


def test_no_letter_or_number_is_refused():
    for s in ("🍳", "#!?", "---", "​", "‍", "́", "🍳 🍳"):
        label, err = em_labels.check_label(s)
        assert label is None and "letter or number" in err, repr(s)


def test_any_script_letter_or_digit_is_enough():
    for s in ("Kitchen", "Küche", "Кухня", "厨房", "مطبخ", "7", "٣", "🍳 Kitchen",
              "Bob's Room", "Kids' room #2", "x" * em_labels.MAX_LABEL_LEN):
        label, err = em_labels.check_label(s)
        assert err is None and label == s, repr(s)


def test_emoji_sequences_with_joiners_are_allowed_beside_a_letter():
    # U+200D is Cf, not Cc: a family emoji must not read as a control character.
    label, err = em_labels.check_label("👨‍👩‍👧 Room")
    assert err is None


def test_the_length_cap_counts_code_points():
    # 32 four-byte characters plus a letter is 33 code points: refused on
    # length, not waved through by a byte or UTF-16 count.
    label, err = em_labels.check_label("a" + "😀" * 32)
    assert label is None and "33" in err
    label, err = em_labels.check_label("a" + "😀" * 31)
    assert err is None


def test_an_accepted_label_fits_the_mdns_txt_record():
    # RFC 6763 §6.1: each TXT string is at most 255 bytes. The widest label
    # the rules allow, in the widest UTF-8, must still fit.
    widest = "a" + "\U0010FFFD" * (em_labels.MAX_LABEL_LEN - 1)
    label, err = em_labels.check_label(widest)
    assert err is None
    assert len(f"friendly_name={label} Voice Assistant".encode()) <= 255
