#!/usr/bin/env python3
"""Pack an emOS release's payload into one bundle.

    make-payload-bundle.py <version> <out.zip> <file> [file ...]

The format lives in controller/em_emos_build.py and is imported from there
rather than reimplemented, for the reason emos/build.sh imports the kernel
architecture sniffer from the same module: the controller READS this archive, so
a second definition of the format here could disagree with the only consumer
there is, and nothing in either tree would notice until a device took a flash.

Imported by path because emos/ sits outside the controller package — the same
trick controller/tests/test_emos_build.py uses in the other direction to load
emos/mkboot.py.
"""
import importlib.util
import pathlib
import sys


def main(argv):
    if len(argv) < 4:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    version, out = argv[1], pathlib.Path(argv[2])
    paths = [pathlib.Path(p) for p in argv[3:]]

    packer = pathlib.Path(__file__).resolve().parents[2] / "controller" / "em_emos_build.py"
    if not packer.is_file():
        print(f"cannot find the packer at {packer} — run this from a full "
              f"checkout rather than a copy of emos/ on its own", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("_eb", packer)
    eb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eb)

    files = {}
    for p in paths:
        if not p.is_file():
            print(f"missing: {p}", file=sys.stderr)
            return 1
        # Keyed on the BASENAME: the bundle is flat, and what the controller
        # looks for is "init32", not wherever the release build happened to
        # leave it.
        files[p.name] = p.read_bytes()

    data = eb.build_payload_bundle(files, version)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    # Read back through the real reader, so the release cannot publish a bundle
    # its own consumer would refuse. The manifest check is the point: this
    # verifies every digest the archive claims.
    back = eb.read_payload_bundle(data)
    if sorted(back["files"]) != sorted(files):
        print("bundle round trip lost files", file=sys.stderr)
        return 1
    for name, blob in files.items():
        if back["files"][name] != blob:
            print(f"bundle round trip corrupted {name}", file=sys.stderr)
            return 1

    print(f"{out} — {len(data):,} bytes, version {version}")
    for name in sorted(files):
        print(f"  {name:<16} {len(files[name]):>9,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
