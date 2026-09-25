"""Device label rules, as a pure function the suite can import.

A label becomes the dashboard name, HA's "<label> Voice Assistant", and a
slug inside every entity_id HA builds for the device. Home Assistant itself
has no maximum for a friendly name (its hard limits are 255 characters for an
entity_id and for a state), so the cap is ours: 32 keeps the HA name and the
entity_ids manageable. It applies when a label is SET — rename or approve —
and never rewrites a label already stored; the dashboard shortens a long
existing one for display instead.

dashboard.jsx mirrors MAX_LABEL_LEN as _MAX_LABEL for the input's maxLength;
tests/test_labels.py fails if the two disagree.

Home Assistant accepts any Unicode in a friendly name, so the refusals beyond
length are what HA takes but should not be given:

- Control characters (Unicode category Cc: C0, DEL, C1). A tab or newline
  passes HA and slugs to an underscore, but it is never meant, and it breaks
  every single-line place the label is shown.
- Lone surrogates (Cs). JSON can carry "\ud800" and json.loads accepts it, but
  the label is later UTF-8 encoded into the mDNS TXT record, and that raises.
- No letter or number at all. HA builds entity_ids by slugifying
  "<label> Voice Assistant", and an emoji- or punctuation-only label slugs to
  nothing, leaving every entity named plain `voice_assistant`.

Format characters (Cf) are allowed on purpose: the zero-width joiner is part
of ordinary emoji sequences. A label made of nothing else still fails the
letter-or-number rule.
"""

import re
import unicodedata

MAX_LABEL_LEN = 32


def check_label(value) -> tuple:
    """Return (label, None) for an acceptable label, or (None, message).

    Surrounding whitespace is stripped before the length is counted, so a
    trailing space pasted with a name cannot be what refuses it.
    """
    if not isinstance(value, str) or not value.strip():
        return None, "Label cannot be empty."
    label = value.strip()
    if len(label) > MAX_LABEL_LEN:
        return None, (f"Label is {len(label)} characters; the maximum is "
                      f"{MAX_LABEL_LEN}.")
    cats = [unicodedata.category(c) for c in label]
    if "Cc" in cats:
        return None, "Label cannot contain line breaks, tabs or other control characters."
    if "Cs" in cats:
        return None, "Label contains invalid text."
    if not any(c.isalnum() for c in label):
        return None, "Label needs at least one letter or number."
    return label, None


def label_key(label: str) -> str:
    """What two labels are compared on to decide they are the same name.

    NFKC folds compatibility forms (fullwidth "Ｋｉｔｃｈｅｎ" is "Kitchen"),
    casefold is Unicode's case-insensitive form ("KÜCHE" and "küche", and
    "Straße" and "STRASSE"), and runs of whitespace collapse to one space, so
    "Kitchen  1" and "Kitchen 1" are the same name. What a person would read
    as the same name is the same name.
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", label).casefold()).strip()


def duplicate_of(label: str, devices, device_id: str):
    """The label of another device that `label` would duplicate, or None.

    `devices` is (device_id, label) pairs; `device_id` is the device being
    named, which is left out so an Echo can be renamed to a new spelling of
    its own name. A device with no label yet (pending) duplicates nothing.
    """
    key = label_key(label)
    for other_id, other in devices:
        if other_id != device_id and other and label_key(other) == key:
            return other
    return None
