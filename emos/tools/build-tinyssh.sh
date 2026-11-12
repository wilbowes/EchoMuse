#!/bin/sh
# Build tinysshd for emOS: static ARM32, the SSH server init runs when the
# owner turns SSH on for that Echo (init.c, ssh_main).
#
# Run inside an armv7 Alpine container, as build-busybox.sh is and for the same
# reasons (static against musl, a native build under qemu, a distro's own
# toolchain):
#
#   ./build-tinyssh.sh [output dir]
#
# WHY TINYSSH. Key-only (Ed25519), ChaCha20-Poly1305, no password code, no
# config file, no listener of its own: it serves one connection on
# stdin/stdout, and init does the accept. Chosen over dropbear 2026-11-03 and
# again 2026-11-11 for that smaller surface. Its multi-call binary picks the
# program from argv[0], so tinysshd-makekey is the same file.
#
# Public domain (CC0), so unlike busybox there is no source to publish beside
# it; the pin below says what it was built from.
set -e

OUT=${1:-$(cd "$(dirname "$0")/.." && pwd)/prebuilt}

[ "$(uname -m)" = armv7l ] || {
    echo "build-tinyssh.sh must run in an armv7 container (uname -m says" >&2
    echo "$(uname -m)). See emos-release.yml." >&2
    exit 1
}

# PINNED by sha512, the value cross-checked against Alpine's APKBUILD for the
# same tag (community/tinyssh, 2026-11-11). 20261101 is a security release
# (strict-KEX, authorized_keys path and PTY fixes); nothing older is
# acceptable.
TS_VER=20261101
TS_SHA512=862d53225f28b5c9f4af9dbd182557abc9da5d5083843302c5c08e415de8ec09212273d35fd069ba7ce305d096a90cf22f0f449f0f295d790c223ce66a924a8c

apk add --no-cache build-base linux-headers >/dev/null

TS_TARBALL=$PWD/tinyssh-$TS_VER.tar.gz
if [ ! -f "$TS_TARBALL" ]; then
    echo "fetching tinyssh $TS_VER"
    wget -q -O "$TS_TARBALL" \
        "https://github.com/janmojzis/tinyssh/archive/refs/tags/$TS_VER.tar.gz"
fi
echo "$TS_SHA512  $TS_TARBALL" | sha512sum -c - >/dev/null || {
    echo "tinyssh tarball does not match its pin -- refusing" >&2
    exit 1
}
[ -d "tinyssh-$TS_VER" ] || tar xzf "$TS_TARBALL"
cd "tinyssh-$TS_VER"

# -no-pie for ET_EXEC, the shape already proven on these kernels (see
# build-busybox.sh). The bundled crypto is used: Alpine's armv7 has no
# lib25519/libntruprime, and trylibs.sh falls back by itself.
make CFLAGS="-W -Wall -Os -fwrapv -Icryptoint -no-pie" \
     LDFLAGS="-static -no-pie" tinysshd >/dev/null

# ---- Verification. Each check is something that fails SILENTLY on hardware.
readelf -l tinysshd | grep -q INTERP && {
    echo "tinysshd is dynamically linked -- refusing" >&2; exit 1; }
readelf -h tinysshd | grep -q "Type:.*EXEC" || {
    echo "tinysshd is not ET_EXEC -- refusing" >&2; exit 1; }
readelf -h tinysshd | grep -q "Machine:.*ARM" || {
    echo "tinysshd is not an ARM binary -- refusing" >&2; exit 1; }

# The multi-call dispatch works here, under qemu, before it has to on an Echo:
# init makes the host key by running this file as tinysshd-makekey.
k=$(mktemp -d)
ln -s "$PWD/tinysshd" "$k/tinysshd-makekey"
ln -s "$PWD/tinysshd" "$k/tinysshd-printkey"
"$k/tinysshd-makekey" "$k/keys"
[ -s "$k/keys/ed25519.pk" ] || {
    echo "tinysshd-makekey made no Ed25519 key -- refusing" >&2; exit 1; }
"$k/tinysshd-printkey" "$k/keys" | grep -q '^ssh-ed25519 ' || {
    echo "tinysshd-printkey printed no ssh-ed25519 key -- refusing" >&2; exit 1; }
rm -rf "$k"

mkdir -p "$OUT"
install -m 0755 tinysshd "$OUT/tinysshd"
echo
printf 'built into %s: tinysshd %d bytes  %s…\n' "$OUT" \
    "$(stat -c%s "$OUT/tinysshd")" "$(sha256sum "$OUT/tinysshd" | cut -c1-16)"
