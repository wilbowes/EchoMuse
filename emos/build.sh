#!/bin/bash
# Build an emOS boot image from a device's OWN boot partition.
#
# The reference image is required and is never shipped: mkboot.py reuses the
# kernel and DTBs out of it, so the artifact is reconstructed on the user's
# side from software already on their device. See README.md.
#
#   ./build.sh <reference boot_a_x.img> [output.img]
#
# Pull the reference off a rooted device first:
#   adb shell su -c 'dd if=/dev/block/mmcblk0p10' > boot_a_x.img
#
# and KEEP IT. It is the recovery image as well as the build input.
set -e

REF=${1:?usage: build.sh <reference boot_a_x.img> [output.img]}
OUT=${2:-emos-boot.img}
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

[ -f "$REF" ] || { echo "reference image not found: $REF" >&2; exit 1; }

# The init is a STATIC binary with no interpreter and no dynamic loader.
# That is not a size optimisation — three separate boots with a busybox
# `#!/bin/busybox sh` init produced no output whatsoever, which is
# indistinguishable from a kernel that never started. See README.md.
NDK=${NDK:-/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin}
CC=${CC:-$NDK/aarch64-linux-android21-clang}

if [ -x "$CC" ]; then
    "$CC" -static -O2 -Wall -o "$WORK/init" "$HERE/init/init.c"
else
    echo "building init in the echomuse-compiler image ($CC not found)"
    docker run --rm -v "$HERE":/emos -v "$WORK":/out -w /emos echomuse-compiler \
        bash -lc "$NDK/aarch64-linux-android21-clang -static -O2 -Wall -o /out/init init/init.c"
fi

# The ramdisk is init plus the empty mountpoints it needs. Everything else the
# system uses is mounted from the device's own /system at runtime, which is why
# no Amazon code is redistributed.
mkdir -p "$WORK/root"/{dev,proc,sys,system,data}
install -m 0755 "$WORK/init" "$WORK/root/init"
( cd "$WORK/root" && find . | cpio -o -H newc 2>/dev/null | gzip -9 ) > "$WORK/ramdisk.gz"

# LK gunzips an AArch64 Image, so the kernel must go back in COMPRESSED — the
# same bytes the reference image carries. Handing it an uncompressed Image
# silently doubles the image and does not boot.
python3 "$HERE/mkboot.py" "$REF" <(python3 - "$REF" <<'EOF'
import struct, sys
ref = open(sys.argv[1], "rb").read()
ksz = struct.unpack("<I", ref[8:12])[0]
kernel = ref[2048:2048 + ksz][0x200:]
i = kernel.find(b"\xd0\x0d\xfe\xed")          # first DTB ends the kernel
sys.stdout.buffer.write(kernel[:i])
EOF
) "$WORK/ramdisk.gz" "$OUT"

echo
echo "built $OUT — flash with:"
echo "  dd if=$OUT of=/dev/block/mmcblk0p10   (boot_a_x on biscuit)"
echo "recover with:"
echo "  dd if=$REF of=/dev/block/mmcblk0p10"
