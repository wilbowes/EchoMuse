#!/bin/sh
# Build e2fsck for emOS: static ARM32, no Android in it at all.
#
# Run inside an armv7 Alpine container (see emos-release.yml), for the reasons
# build-busybox.sh gives: a static binary needs only the kernel's syscall ABI,
# and Alpine's gcc + musl is a distribution's toolchain, native under qemu.
#
#   ./build-e2fsck.sh [output dir]
#
# WHY EMOS CARRIES ITS OWN. init checks /data with `e2fsck -p` before mounting
# it read-write, and ran Amazon's /system/bin/e2fsck to do it. The Dot 3
# (donut) has none: FireOS 6.5.5.5 and 6.5.7.5 on 41C8 ship only
# vendor/bin/fsck_msdos_mtk, so every donut boot since 2026-11-03 mounted /data
# unchecked and said nothing (found by the wizard's donor gate, 2026-11-15).
# Ours fixes that on every board and removes one more Amazon dependency.
#
# Only e2fsck is built: no mke2fs, debugfs, resize2fs, fuse2fs or NLS. Its
# libraries (ext2fs, com_err, e2p, and e2fsprogs' own blkid and uuid) are
# linked in statically from the same tree.
set -e

OUT=${1:-$(cd "$(dirname "$0")/.." && pwd)/prebuilt}

[ "$(uname -m)" = armv7l ] || {
    echo "build-e2fsck.sh must run in an armv7 container (uname -m says" >&2
    echo "$(uname -m)). See emos-release.yml." >&2
    exit 1
}

# PINNED by sha512, and the value is ALPINE's published sum for the same
# tarball (aports main/e2fsprogs/APKBUILD), cross-checked rather than taken
# from one download of ours. Same rule as busybox and hostap.
E2_VER=1.47.4
E2_SHA512=923c1ba52cf48557e190ee936be47ff3e43e4798950499e3c663f0f077470caefbb69ee845ebe1f969b3e7c1bb0c22e507188f1daca24649c54c5843c835b2f9

apk add --no-cache build-base linux-headers >/dev/null

# The tarball is also the corresponding source: e2fsck is GPL-2.0, so this
# exact file is published beside the binary, as busybox's is. Fetched whether
# or not a tree is already unpacked, so a pre-seeded tree can never produce a
# binary with no source to ship.
TARBALL=e2fsprogs-$E2_VER.tar.gz
[ -f "$TARBALL" ] || wget -q -O "$TARBALL" \
    "https://github.com/tytso/e2fsprogs/archive/v$E2_VER.tar.gz"
echo "$E2_SHA512  $TARBALL" | sha512sum -c - >/dev/null || {
    echo "$TARBALL does not match the pinned sha512" >&2
    exit 1
}
rm -rf "e2fsprogs-$E2_VER"
tar xzf "$TARBALL"
cd "e2fsprogs-$E2_VER"

# --disable-* trims what would otherwise be built and never shipped; the
# internal blkid/uuid are e2fsprogs' own, so no util-linux (and none of its
# LGPL relinking terms) enters the binary. Alpine's package links util-linux's
# shared libs instead, which is fine for a package and wrong for a static one.
#
# lseek64 on musl: 1.2.4 dropped the SYMBOL and keeps it only as a macro for
# lseek (64-bit off_t) under _LARGEFILE64_SOURCE. configure's link test looks
# for the symbol, finds none, and the bundled blkid then falls back to a raw
# _syscall5 shim that does not compile (llseek.c, "unknown type name
# 'result'"). So the define goes in CFLAGS, and the function check is answered
# yes: with the define, every lseek64 in the tree is musl's lseek.
ac_cv_func_lseek64=yes \
CFLAGS="-O2 -D_LARGEFILE64_SOURCE" LDFLAGS=-static ./configure --disable-nls --disable-fuse2fs --disable-uuidd \
    --disable-debugfs --disable-imager --disable-resizer --disable-defrag \
    --disable-elf-shlibs --disable-tdb --disable-mmp --without-libarchive \
    --enable-libblkid --enable-libuuid >/dev/null
make -j"$(nproc)" libs >/dev/null
make -j"$(nproc)" -C e2fsck e2fsck LDFLAGS=-static >/dev/null
strip e2fsck/e2fsck

mkdir -p "$OUT"
cp e2fsck/e2fsck "$OUT/e2fsck"
cp NOTICE "$OUT/e2fsprogs-NOTICE"
cp "../$TARBALL" "$OUT/$TARBALL"

# Loud rather than subtly wrong, as for busybox: it must be static, 32-bit ARM
# and run.
file "$OUT/e2fsck" 2>/dev/null || true
"$OUT/e2fsck" -V 2>&1 | head -1
