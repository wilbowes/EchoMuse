"""ARPAbet phonemes for words the CMU dictionary does not have, from espeak.

openWakeWord's generate_adversarial_texts needs phonemes for an
out-of-dictionary word ("hanako", "clarra") to find real words that sound
like it, which become negatives. It got them from DeepPhonemizer's
en_us_cmudict_forward.pt, downloaded at first use from an S3 bucket that
answers 403 since 2026 and has no mirror we could find (the Hugging Face
copy is the IPA model, not a drop-in). The download also landed inside the
container, so recreating the container lost the copy that had worked.

espeak-ng is already in the image (piper_phonemize, which Piper itself
uses), so this maps its IPA onto the ARPAbet set CMUdict uses and answers in
DeepPhonemizer's "[HH][AH][N]" format, which data.py then strips. It only
has to be close: the phonemes seed a fuzzy search for similar-sounding
dictionary words, and stress is discarded before that search.
tests/test_forge_g2p.py measures it against CMUdict itself.
"""

# Longest first, so diphthongs and affricates win over their parts.
IPA_TO_ARPABET = {
    "aɪə": "AY ER", "aʊə": "AW ER",
    "aɪ": "AY", "aʊ": "AW", "eɪ": "EY", "oʊ": "OW", "əʊ": "OW", "ɔɪ": "OY",
    "tʃ": "CH", "dʒ": "JH", "ɑː": "AA", "ɔː": "AO", "ɜː": "ER", "iː": "IY",
    "uː": "UW", "ɪə": "IH R", "ɛə": "EH R", "ʊə": "UH R", "oː": "AO", "aː": "AA",
    "ɑ": "AA", "æ": "AE", "ʌ": "AH", "ə": "AH", "ɐ": "AH", "ɔ": "AO", "ɛ": "EH",
    "e": "EH", "ɜ": "ER", "ɚ": "ER", "ɝ": "ER", "ɪ": "IH", "ᵻ": "IH", "i": "IY",
    "ʊ": "UH", "u": "UW", "o": "OW", "a": "AA", "ɒ": "AA",
    "b": "B", "d": "D", "ð": "DH", "f": "F", "ɡ": "G", "g": "G", "h": "HH",
    "k": "K", "l": "L", "ɫ": "L", "m": "M", "n": "N", "ŋ": "NG", "p": "P",
    "ɹ": "R", "r": "R", "ɾ": "D", "s": "S", "ʃ": "SH", "t": "T", "θ": "TH",
    "v": "V", "w": "W", "j": "Y", "z": "Z", "ʒ": "ZH", "x": "HH", "ç": "HH",
    "ʔ": "", "ɬ": "L",
}
_KEYS = sorted(IPA_TO_ARPABET, key=len, reverse=True)
# Stress and syllable marks, and the syllabic diacritic (n̩ = AH N).
_SKIP = {"ˈ", "ˌ", ".", "-", " ", "̃"}


def ipa_to_arpabet(ipa: str) -> list:
    out, i = [], 0
    while i < len(ipa):
        c = ipa[i]
        if c in _SKIP:
            i += 1
            continue
        if c == "̩":          # syllabic: the consonant before carries a vowel
            if out:
                out.insert(len(out) - 1, "AH")
            i += 1
            continue
        for k in _KEYS:
            if ipa.startswith(k, i):
                out.extend(IPA_TO_ARPABET[k].split())
                i += len(k)
                break
        else:
            i += 1                  # a symbol with no English equivalent: drop it
    return out


def arpabet(word: str) -> list:
    import piper_phonemize  # here, so the mapping is testable without espeak
    ipa = "".join("".join(p) for p in piper_phonemize.phonemize_espeak(word, "en-us"))
    return ipa_to_arpabet(ipa)


class Phonemizer:
    """Stands in for dp.phonemizer.Phonemizer as data.py calls it."""

    def __call__(self, word: str, lang: str = "en_us") -> str:
        return "".join(f"[{p}]" for p in arpabet(word))
