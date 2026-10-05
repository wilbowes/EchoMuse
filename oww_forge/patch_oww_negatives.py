"""Filter openWakeWord's adversarial negatives through forge_negatives before
train.py synthesises them (see forge_negatives.py for why). One line goes in
ahead of each of the two generate_samples calls, train and validation. Each
must be found exactly once, or the build fails: an upstream change should
stop the build, not quietly bring the bare-name negatives back."""
import sys
from pathlib import Path

ANCHORS = [
    '            generate_samples(text=adversarial_texts, max_samples=config["n_samples"]-n_current_samples,',
    '            generate_samples(text=adversarial_texts, max_samples=config["n_samples_val"]-n_current_samples,',
]
FILTER = ('            from openwakeword.forge_negatives import drop_targets; '
          'adversarial_texts = drop_targets(adversarial_texts, config["target_phrase"])')


def patch(src: str) -> str:
    # The pinned train.py has CRLF line endings; keep whatever the file uses.
    newline = "\r\n" if "\r\n" in src else "\n"
    for anchor in ANCHORS:
        if src.count(anchor) != 1:
            sys.exit(f"patch_oww_negatives: expected exactly one of {anchor!r}")
        src = src.replace(anchor, FILTER + newline + anchor)
    return src


if __name__ == "__main__":
    path = Path(sys.argv[1])
    path.write_bytes(patch(path.read_bytes().decode()).encode())
