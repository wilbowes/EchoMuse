"""
Trusting a private CA, for a Home Assistant served over HTTPS with one.

The failure this fixes is quiet from the user's side: the controller starts
cleanly, the device wakes, and then every turn ends with no audio because the
TTS fetch could not verify the certificate. So the tests here are mostly about
refusing to look like it worked.
"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

import em_cacerts

PEM = ("-----BEGIN CERTIFICATE-----\n"
       "MIIByjCCATOgAwIBAgIUFakeFakeFake\n"
       "-----END CERTIFICATE-----\n")


def _ok(*args, **kwargs):
    class R:
        returncode = 0
        stdout = "1 added"
        stderr = ""
    return R()


def _fails(*args, **kwargs):
    class R:
        returncode = 1
        stdout = ""
        stderr = "update-ca-certificates: broken"
    return R()


def test_a_pem_certificate_is_installed_and_the_store_rebuilt(tmp_path):
    src = tmp_path / "internal-ca.crt"
    src.write_text(PEM)
    anchors = tmp_path / "anchors"
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return _ok()

    msg = em_cacerts.install(str(src), anchor_dir=anchors, runner=runner)
    assert (anchors / "internal-ca.crt").read_text() == PEM
    assert calls == [["update-ca-certificates"]]
    assert "internal-ca.crt" in msg


def test_a_pem_extension_is_renamed_to_crt(tmp_path):
    """
    update-ca-certificates processes ONLY *.crt in its anchor directory, and
    .pem is the more common extension by far. Copying the file under its own
    name would have it silently skipped — the store rebuilds, the command
    succeeds, and nothing is trusted.
    """
    src = tmp_path / "internal-ca.pem"
    src.write_text(PEM)
    anchors = tmp_path / "anchors"
    em_cacerts.install(str(src), anchor_dir=anchors, runner=_ok)
    assert (anchors / "internal-ca.crt").is_file()
    assert not (anchors / "internal-ca.pem").exists()


def test_anchor_name_always_ends_crt():
    assert em_cacerts.anchor_name("/ssl/ca.pem") == "ca.crt"
    assert em_cacerts.anchor_name("/ssl/ca.crt") == "ca.crt"
    assert em_cacerts.anchor_name("/ssl/ca.CRT") == "ca.CRT"
    assert em_cacerts.anchor_name("/ssl/internal") == "internal.crt"


def test_a_missing_file_is_refused_with_somewhere_to_look(tmp_path):
    with pytest.raises(em_cacerts.CATrustError) as e:
        em_cacerts.install(str(tmp_path / "nope.crt"),
                           anchor_dir=tmp_path / "a", runner=_ok)
    # The message has to say where to put it, or the user is left guessing
    # between the host path and the path inside the container.
    assert "/ssl" in str(e.value)


def test_a_non_pem_file_is_refused_with_the_conversion_command(tmp_path):
    """
    update-ca-certificates accepts a DER file without complaint and produces
    a bundle that does not contain the CA — the original failure, with a
    successful-looking startup in front of it.
    """
    src = tmp_path / "ca.crt"
    src.write_bytes(b"\x30\x82\x01\x0a\x02\x82")      # DER, not PEM
    with pytest.raises(em_cacerts.CATrustError) as e:
        em_cacerts.install(str(src), anchor_dir=tmp_path / "a", runner=_ok)
    assert "openssl" in str(e.value)


def test_a_failing_update_is_reported_not_swallowed(tmp_path):
    src = tmp_path / "ca.crt"
    src.write_text(PEM)
    with pytest.raises(em_cacerts.CATrustError) as e:
        em_cacerts.install(str(src), anchor_dir=tmp_path / "a", runner=_fails)
    assert "broken" in str(e.value)


def test_looks_like_pem():
    assert em_cacerts.looks_like_pem(PEM)
    assert not em_cacerts.looks_like_pem("-----BEGIN PRIVATE KEY-----\n")
    assert not em_cacerts.looks_like_pem("")


def test_startup_refuses_rather_than_starting_untrusted():
    """
    em_start exits non-zero on a bad certificate. Starting anyway would give a
    controller that looks healthy and fails every voice turn on a TLS error
    nobody connects back to this option.
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "em_start.py").read_text()
    ca = src[src.index("EM_EXTRA_CA_CERT"):]
    assert "sys.exit(1)" in ca
    assert "em_cacerts.install" in ca
