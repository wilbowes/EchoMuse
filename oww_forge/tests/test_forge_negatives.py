"""A target phrase must never be synthesised as an adversarial negative (see
forge_negatives.py). The filter is pure and tested on the texts openWakeWord
actually produced for the two models that showed the fault; the build patch
is tested on the exact lines it has to find in the pinned train.py."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import forge_negatives  # noqa: E402
import patch_oww_negatives  # noqa: E402

VERONA = ["hey verona", "verona"]
MIRAKO = ["hi mirako", "hello mirako", "hey mirako", "mirako"]


@pytest.mark.parametrize("targets,text", [
    (VERONA, "verona"),          # the bare name, 2.7% of the negatives
    (VERONA, "verona hey"),      # contains it
    (VERONA, "Verona"),
    (VERONA, "  verona  hey "),
    (MIRAKO, "mirako"),          # three longer phrases each emit it
    (MIRAKO, "mirako hello"),
    (["hey verona"], "oh hey verona there"),
])
def test_a_negative_containing_a_target_is_dropped(targets, text):
    assert forge_negatives.drop_targets([text], targets) == []


@pytest.mark.parametrize("targets,text", [
    (VERONA, "hey"),             # half of a phrase is a fair negative
    (VERONA, "verina"),          # a near miss is the point of these
    (VERONA, "misbehaved"),
    (MIRAKO, "mirado"),
    (MIRAKO, "hello"),
    (["hey verona"], "verona"),  # not a target here, so upstream's intent stands
    (["hey verona"], "verona hey"),
    (["verona"], "veronas"),     # whole words only
])
def test_other_negatives_are_kept(targets, text):
    assert forge_negatives.drop_targets([text], targets) == [text]


def test_order_survives_and_empty_inputs_are_safe():
    assert forge_negatives.drop_targets(["hey", "verona", "verina"], VERONA) == ["hey", "verina"]
    assert forge_negatives.drop_targets(None, VERONA) == []
    assert forge_negatives.drop_targets(["hey"], None) == ["hey"]
    assert forge_negatives.drop_targets(["hey"], [""]) == ["hey"]


# The two call sites as they stand in the pinned train.py, CRLF included.
TRAIN_PY = (
    '            adversarial_texts = config["custom_negative_phrases"]\r\n'
    '            generate_samples(text=adversarial_texts, max_samples=config["n_samples"]-n_current_samples,\r\n'
    '                             batch_size=config["tts_batch_size"]//7,\r\n'
    '            adversarial_texts = config["custom_negative_phrases"]\r\n'
    '            generate_samples(text=adversarial_texts, max_samples=config["n_samples_val"]-n_current_samples,\r\n'
)


@pytest.mark.parametrize("src", [TRAIN_PY, TRAIN_PY.replace("\r\n", "\n")])
def test_patch_filters_before_both_generate_calls(src):
    out = patch_oww_negatives.patch(src)
    newline = "\r\n" if "\r\n" in src else "\n"
    lines = out.split(newline)
    calls = [i for i, l in enumerate(lines) if "generate_samples(text=adversarial_texts" in l]
    assert len(calls) == 2
    for i in calls:
        assert 'drop_targets(adversarial_texts, config["target_phrase"])' in lines[i - 1]
    assert "\r" not in out.replace("\r\n", "")


def test_patch_fails_loudly_when_upstream_changes():
    with pytest.raises(SystemExit):
        patch_oww_negatives.patch(TRAIN_PY.replace("n_samples_val", "n_val"))
