"""Keep the target phrases out of the adversarial negatives.

openWakeWord builds hard negatives per target phrase from partial phrases and
kept input words, and only removes texts equal to THAT phrase. Train "hey
verona" beside "verona" and the first emits "verona" (and "verona hey") as
negatives: the bare name becomes a positive and one of the most common
negatives at once, and stops firing. Counted over 20k texts, 2026-10-03:
"verona" was 2.7% of them.

Copied into the openwakeword package by the Dockerfile; train.py calls it
through patch_oww_negatives."""


def _words(text: str) -> list[str]:
    return text.lower().split()


def contains_target(text: str, targets) -> bool:
    """True when any target phrase occurs in text as whole, adjacent words."""
    words = _words(text)
    for target in targets:
        t = _words(target)
        if t and any(words[i:i + len(t)] == t for i in range(len(words) - len(t) + 1)):
            return True
    return False


def drop_targets(texts, targets) -> list[str]:
    targets = list(targets or [])
    return [t for t in (texts or []) if not contains_target(t, targets)]
