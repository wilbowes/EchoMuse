#!/usr/bin/env sh
# make_dev_addon.sh — package the working tree as a LOCAL Home Assistant add-on.
#
# Supervisor clones the repository's DEFAULT BRANCH for a normal add-on, so a
# branch cannot be installed — which means add-on changes are untestable until
# they are on main, exactly backwards for anything add-on-specific (ingress,
# options, /data, auth). A local add-on is the way round it: Supervisor builds
# whatever sits in /addons/<folder> on the Home Assistant host.
#
# This generates that folder from the real config.yaml rather than asking
# anyone to hand-edit it. Hand-editing is how the dev copy and the shipped one
# drift, and a divergence in exactly this file is what shipped a controller
# with no ingress support (#160).
#
# Usage, from controller/:
#     tools/make_dev_addon.sh [output-dir]
#
# Then copy the result to /addons/ on the Home Assistant host (the `addons`
# Samba share, or scp via the SSH add-on) and look in
# Settings -> Add-ons -> Add-on Store; local add-ons appear at the top.
#
# Nothing here compiles: ffmpeg is apt, and onnxruntime, speexdsp-ns, scipy
# and scikit-learn are all prebuilt wheels. The build is downloads, so it
# costs minutes rather than the ordeal config.yaml's comment implies.
set -eu

SRC="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUT="${1:-$SRC/../dist/echomuse-dev}"

[ -f "$SRC/config.yaml" ] || { echo "no config.yaml in $SRC" >&2; exit 1; }

rm -rf "$OUT"
mkdir -p "$OUT"

# The build context is the controller directory. Copy it wholesale minus the
# things that must not travel: a local database and TLS material would
# overwrite the add-on's own, and __pycache__ from a different Python is at
# best noise.
tar -cf - -C "$SRC" \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='data' \
    --exclude='*.db' \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    --exclude='recordings' \
    --exclude='tls' \
    . | tar -xf - -C "$OUT"

# --match: the nearest tag of ANY kind is often emos-v*, which names the wrong
# component and looks plausible (the build.sh trap in the root CLAUDE.md).
VERSION="dev-$(git -C "$SRC" describe --tags --match 'controller-v*' --match 'controller-ea-v*' --always --dirty 2>/dev/null || echo local)"

# Rewrite the add-on identity:
#   image:   REMOVED, so Supervisor builds this folder instead of pulling a
#            published tag. This is the one intentional divergence, and it is
#            the whole point of a local add-on.
#   slug:    distinct, so this installs alongside the released add-on rather
#            than replacing it.
#   version: the git description, so the add-on page says what it is.
python3 - "$OUT/config.yaml" "$VERSION" <<'PY'
import re, sys
path, version = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()

# Drop `image:` and the comment block explaining it — that comment is about
# pulling a published image, which this build deliberately does not do.
text = re.sub(r'(?:^#.*\n)*^image:.*\n', '', text, count=1, flags=re.M)

text = re.sub(r'^name:.*$',    'name: "EchoMuse (dev)"',   text, count=1, flags=re.M)
text = re.sub(r'^slug:.*$',    'slug: "controller-dev"',   text, count=1, flags=re.M)
text = re.sub(r'^version:.*$', f'version: "{version}"',    text, count=1, flags=re.M)

# ESPHome on x2xx (GA x0xx, EA x1xx): HA keys a satellite on host and port, so
# sharing a range would point HA's stored entries at whichever device holds
# that number under dev. BLE (17201+) follows by the existing offset. The
# controller's own ports stay: GA, EA and dev are never run together.
text = re.sub(r'^(  esphome_port_base:) \d+$', r'\1 16201', text, count=1, flags=re.M)

open(path, "w", encoding="utf-8").write(text)

# Check the KEY, not the substring — several comments in this file mention
# `image:` while explaining the pin, and matching those made the guard fail
# on a rewrite that had worked.
missing = [k for k in ("slug:", "version:", "ingress:", "  esphome_port_base: 16201")
           if not re.search(rf'^{k}', text, re.M)]
if missing or re.search(r'^image:', text, re.M):
    sys.exit(f"config.yaml rewrite failed: {missing or 'image: key still present'}")
PY

echo "Local add-on written to: $OUT"
echo "  version: $VERSION"
echo
echo "Copy it to /addons/ on the Home Assistant host, then reload the store."
echo
echo "ESPHome satellites from 16201 (BLE 17201), so HA's entries for dev"
echo "devices never collide with GA's or EA's."
echo
echo "NOTE: an add-on gets its OWN /data, so this starts with an EMPTY"
echo "database and a FRESHLY GENERATED CA. A device holding the released"
echo "add-on's CA will dial wss, fail verification and never connect — it"
echo "picks wss from the mDNS tls_port record, not from require_device_tls,"
echo "so turning that off does not help. To test against a real device,"
echo "copy the released add-on's /data (at minimum tls/, all four files)"
echo "into the dev add-on's /data before starting it."
echo
echo "Both use host networking and the same ports, so stop one before"
echo "starting the other."
