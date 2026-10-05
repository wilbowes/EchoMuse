"""forge_g2p stands in for DeepPhonemizer's dead checkpoint (see its
docstring). The IPA-to-ARPAbet mapping is pure and tested here against
espeak's own output for words CMUdict knows; the build patch is tested on
the exact lines it has to find in openWakeWord's data.py."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import forge_g2p  # noqa: E402
import patch_oww_g2p  # noqa: E402

# espeak-ng en-us output (as piper_phonemize returns it, joined) → CMUdict
# without stress. Captured from the forge image 2026-09-28.
CASES = [
    ("həlˈoʊ", "HH AH L OW"),                 # hello
    ("kˈɪtʃən", "K IH CH AH N"),              # kitchen
    ("θˈɔːt", "TH AO T"),                     # thought
    ("mˈɛʒɚ", "M EH ZH ER"),                  # measure
    ("dʒˈʌdʒ", "JH AH JH"),                   # judge
    ("sˈɪŋɪŋ", "S IH NG IH NG"),              # singing
    ("mɪɹˈɑːkoʊ", "M IH R AA K OW"),          # mirako: made up, not captured
    ("vəɹˈoʊnə", "V AH R OW N AH"),           # verona
    ("wˈɜːkɚ", "W ER K ER"),                  # worker
]


@pytest.mark.parametrize("ipa,arpa", CASES)
def test_ipa_to_arpabet(ipa, arpa):
    assert forge_g2p.ipa_to_arpabet(ipa) == arpa.split()


def test_output_is_only_cmudict_symbols():
    cmu = set("AA AE AH AO AW AY B CH D DH EH ER EY F G HH IH IY JH K L M N NG OW OY P R S SH T TH UH UW V W Y Z ZH".split())
    for arpa in forge_g2p.IPA_TO_ARPABET.values():
        assert set(arpa.split()) <= cmu, arpa


def test_phonemizer_answers_in_deepphonemizers_format(monkeypatch):
    # data.py strips "][" and brackets: "[HH][AE]" → "HH AE".
    monkeypatch.setattr(forge_g2p, "arpabet", lambda w: ["HH", "AE"])
    assert forge_g2p.Phonemizer()("x", lang="en_us") == "[HH][AE]"


DATA_PY = """        if not os.path.exists(phonemizer_mdl_path):
            download()

        # Create phonemizer object
        from dp.phonemizer import Phonemizer
        phonemizer = Phonemizer.from_checkpoint(phonemizer_mdl_path)
"""


def test_patch_replaces_the_download_and_the_model():
    out = patch_oww_g2p.patch(DATA_PY)
    assert "if False:" in out and "from openwakeword.forge_g2p import Phonemizer" in out
    assert "from_checkpoint" not in out


def test_patch_fails_loudly_when_upstream_changes():
    with pytest.raises(SystemExit):
        patch_oww_g2p.patch(DATA_PY.replace("from_checkpoint", "load"))
