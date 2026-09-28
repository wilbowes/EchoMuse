"""Point openWakeWord's data.py at forge_g2p instead of DeepPhonemizer's
dead S3 checkpoint (see the Dockerfile and forge_g2p.py for why). Every edit
must land exactly once, or the build fails: an upstream change should stop
the build, not quietly bring the download back."""
import sys
from pathlib import Path

EDITS = [
    ("        if not os.path.exists(phonemizer_mdl_path):",
     "        if False:  # oww_forge: the S3 checkpoint is gone; forge_g2p stands in"),
    ("        from dp.phonemizer import Phonemizer\n"
     "        phonemizer = Phonemizer.from_checkpoint(phonemizer_mdl_path)",
     "        from openwakeword.forge_g2p import Phonemizer\n"
     "        phonemizer = Phonemizer()"),
]


def patch(src: str) -> str:
    for old, new in EDITS:
        if src.count(old) != 1:
            sys.exit(f"patch_oww_g2p: expected exactly one of {old!r}")
        src = src.replace(old, new)
    return src


if __name__ == "__main__":
    path = Path(sys.argv[1])
    path.write_text(patch(path.read_text()))
