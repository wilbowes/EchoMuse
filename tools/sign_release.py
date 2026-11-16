#!/usr/bin/env python3
"""Sign a draft release's installable assets and publish it.

The controller installs only assets whose signature verifies against a key in
controller/em_release_sig.py (once one is there). This is the one command run
per firmware or emOS release, on a machine that holds the private key. Never
run it on a machine that holds the repo's push credentials alone; the key and
the GitHub login are meant to be two separate things to steal.

  sign_release.py keygen KEY.pem      new passphrase-protected key; prints the
                                      public key for em_release_sig.KEYS
  sign_release.py sign TAG --key KEY.pem [--no-publish]

`sign` downloads the release's installable assets, refuses any that GitHub's
build attestation does not tie to this repo's workflows (so a swapped asset is
never signed), uploads `<asset>.sig`, and publishes the draft. Needs `gh`.
"""
import argparse
import base64
import getpass
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "controller"))
import em_release_sig  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

REPO = "wilbowes/EchoMuse"
# What the controller downloads and installs: firmware, the loose emOS init,
# and the emOS bundle (init32 and the rest travel inside it).
INSTALLABLE = ("server", "init", "emos-payload.zip")


def gh(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(["gh", *args], check=True, cwd=cwd,
                          capture_output=True, text=True).stdout


def public_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def keygen(out: Path) -> None:
    if out.exists():
        sys.exit(f"{out} exists; refusing to overwrite a key")
    pw = getpass.getpass("New key passphrase: ").encode()
    if not pw or pw != getpass.getpass("Again: ").encode():
        sys.exit("passphrases empty or different")
    key = Ed25519PrivateKey.generate()
    out.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(pw)))
    out.chmod(0o600)
    print(f"Wrote {out}. Back it up offline. Public key for KEYS:\n{public_b64(key)}")


def load(path: Path) -> Ed25519PrivateKey:
    data = path.read_bytes()
    try:
        key = serialization.load_pem_private_key(data, None)
    except TypeError:
        key = serialization.load_pem_private_key(
            data, getpass.getpass(f"Passphrase for {path}: ").encode())
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit(f"{path} is not an Ed25519 key")
    return key


def sign(tag: str, key_path: Path, publish: bool) -> None:
    key = load(key_path)
    pub = public_b64(key)
    if pub not in em_release_sig.KEYS:
        print(f"warning: this key ({pub}) is not in em_release_sig.KEYS; "
              "controllers will not accept its signatures", file=sys.stderr)
    names = {a for a in gh("release", "view", tag, "-R", REPO, "--json", "assets",
                           "--jq", ".assets[].name").split()}
    todo = [n for n in INSTALLABLE if n in names]
    if not todo:
        sys.exit(f"{tag} has none of {', '.join(INSTALLABLE)}")
    with tempfile.TemporaryDirectory() as d:
        sigs = []
        for name in todo:
            gh("release", "download", tag, "-R", REPO, "-p", name, "-D", d)
            path = Path(d) / name
            # Only sign what this repo's own workflow built.
            gh("attestation", "verify", str(path), "-R", REPO)
            sig = Path(d) / (name + em_release_sig.SIG_SUFFIX)
            sig.write_text(base64.b64encode(
                key.sign(em_release_sig.message(tag, name, path.read_bytes()))).decode() + "\n")
            url = f"https://github.com/{REPO}/releases/download/{tag}/{name}"
            assert em_release_sig.problem(url, path.read_bytes(), sig.read_bytes(),
                                          keys=(pub,)) is None
            sigs.append(str(sig))
            print(f"signed {name}")
        gh("release", "upload", tag, "-R", REPO, *sigs)
    if publish:
        gh("release", "edit", tag, "-R", REPO, "--draft=false")
        print(f"published {tag}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("out", type=Path)
    s = sub.add_parser("sign")
    s.add_argument("tag")
    s.add_argument("--key", type=Path, required=True)
    s.add_argument("--no-publish", action="store_true")
    a = ap.parse_args()
    if a.cmd == "keygen":
        keygen(a.out)
    else:
        sign(a.tag, a.key, not a.no_publish)


if __name__ == "__main__":
    main()
