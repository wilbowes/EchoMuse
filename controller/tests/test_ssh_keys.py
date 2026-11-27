"""
The controller's SSH key check agrees with the Echo's, case for case.

Both read device/internal/config/testdata/ssh_keys.json, written from the
spec's edges (RFC 8709 §4, RFC 4253 §6.6): the key type, the 32-byte key,
nothing trailing, canonical base64, options refused, the comment's character
class and length, the list's length.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import em_ssh_keys

VECTORS = json.loads((Path(__file__).resolve().parents[2] / "device" / "internal"
                      / "config" / "testdata" / "ssh_keys.json").read_text())


@pytest.mark.parametrize("case", VECTORS["accept"], ids=lambda c: c["name"])
def test_accepted(case):
    assert em_ssh_keys.parse(case["text"]) == case["keys"]


@pytest.mark.parametrize("case", VECTORS["refuse"], ids=lambda c: c["name"])
def test_refused(case):
    with pytest.raises(em_ssh_keys.KeyError_) as e:
        em_ssh_keys.parse(case["text"])
    assert e.value.line == case["line"]


def test_a_key_openssh_made_is_accepted_unchanged(tmp_path):
    kg = shutil.which("ssh-keygen")
    if not kg:
        pytest.skip("no ssh-keygen")
    subprocess.run([kg, "-q", "-t", "ed25519", "-N", "", "-C", "me@host",
                    "-f", str(tmp_path / "k")], check=True)
    pub = (tmp_path / "k.pub").read_text().strip()
    assert em_ssh_keys.parse(pub) == [pub]
    subprocess.run([kg, "-q", "-t", "rsa", "-b", "2048", "-N", "",
                    "-f", str(tmp_path / "r")], check=True)
    with pytest.raises(em_ssh_keys.KeyError_):
        em_ssh_keys.parse((tmp_path / "r.pub").read_text())
