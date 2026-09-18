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
"""

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
    return label, None
