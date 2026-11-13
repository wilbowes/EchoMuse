"""patch_xpu moves upstream's CUDA-only device code to Intel's XPU, in the
GPU=xpu image only, and stops the build if upstream has moved."""

import re
import sys
from pathlib import Path

import pytest

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE))
import patch_xpu  # noqa: E402

# The call sites as they stand in the pinned sources (train.py is CRLF).
TRAIN_PY = (
    "        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')\r\n"
    + "            torch.cuda.empty_cache()\r\n" * 4
    + '                                            device="gpu" if torch.cuda.is_available() else "cpu",\r\n'
)
DATA_PY = "        device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')\n"
GENERATE_PY = """    if torch.cuda.is_available():
        model.cuda()
                    except torch.cuda.OutOfMemoryError:
                        torch.cuda.empty_cache()
    if torch.cuda.is_available():
        speaker_1 = speaker_1.cuda()
        speaker_2 = speaker_2.cuda()
        x = x.cuda()
        x_lengths = x_lengths.cuda()
"""


def test_training_and_augmentation_run_on_xpu():
    train = patch_xpu.patch("train.py", TRAIN_PY)
    assert "torch.device('xpu' if torch.xpu.is_available() else 'cpu')" in train
    assert train.count("torch.xpu.empty_cache()") == 4
    # Feature extraction keeps asking for CUDA, so on XPU it takes the CPU branch.
    assert 'device="gpu" if torch.cuda.is_available()' in train
    assert "\r" not in train.replace("\r\n", "")
    assert "xpu" in patch_xpu.patch("data.py", DATA_PY)


def test_piper_generation_runs_on_xpu():
    out = patch_xpu.patch("generate_samples.py", GENERATE_PY)
    assert "cuda" not in out
    assert out.count("if torch.xpu.is_available():") == 2
    assert "except torch.OutOfMemoryError:" in out


@pytest.mark.parametrize("name,src,broken", [
    ("train.py", TRAIN_PY, TRAIN_PY.replace("'cuda:0'", "'cuda'")),
    ("generate_samples.py", GENERATE_PY, GENERATE_PY.replace("model.cuda()", "model.to('cuda')")),
    ("generate_samples.py", GENERATE_PY, GENERATE_PY + "        y = y.cuda()\n"),
])
def test_an_upstream_change_fails_the_build(name, src, broken):
    patch_xpu.patch(name, src)
    with pytest.raises(SystemExit):
        patch_xpu.patch(name, broken)


def test_only_the_xpu_image_is_patched():
    text = (FORGE / "Dockerfile").read_text()
    run = re.search(r"RUN if \[ \"\$GPU\" = \"xpu\" \]; then \\\n(?:.*\n)*?.*patch_xpu\.py", text)
    assert run, "patch_xpu.py must run only under GPU=xpu"
