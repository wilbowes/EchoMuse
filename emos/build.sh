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

# The init must match the REFERENCE kernel's architecture. FireOS 5 boots a
# 64-bit (AArch64) kernel and FireOS 6 a 32-bit ARM one — the same 3.18.19
# source, compiled both ways — and an init of the wrong architecture boots to
# nothing at all, with no output. So it is read out of the reference rather
# than assumed: a zImage (magic 0x016f2818 at 0x24) is ARM, a raw gzip stream
# whose Image carries "ARM\x64" at 0x38 is AArch64. Anything else is refused.
ARCH=$(python3 - "$REF" <<'EOF'
import struct, sys, zlib
ref = open(sys.argv[1], "rb").read()
ksz = struct.unpack("<I", ref[8:12])[0]
p = ref[2048:2048 + ksz][0x200:]
if p[0x24:0x28] == b"\x18\x28\x6f\x01":
    print("arm")
elif p[:2] == b"\x1f\x8b" and \
        zlib.decompressobj(31).decompress(p, 0x40)[0x38:0x3c] == b"ARM\x64":
    print("arm64")
else:
    print("unknown")
EOF
)
case "$ARCH" in
    arm64) TRIPLE=aarch64-linux-android21 ;;
    arm)   TRIPLE=armv7a-linux-androideabi21 ;;
    *)     echo "cannot tell the reference kernel's architecture — not building" >&2
           exit 1 ;;
esac
echo "reference kernel is $ARCH: building a matching init"
CC=${CC:-$NDK/$TRIPLE-clang}

if [ -x "$CC" ]; then
    "$CC" -static -O2 -Wall -o "$WORK/init" "$HERE/init/init.c"
else
    echo "building init in the echomuse-compiler image ($CC not found)"
    docker run --rm -v "$HERE":/emos -v "$WORK":/out -w /emos echomuse-compiler \
        bash -lc "$NDK/$TRIPLE-clang -static -O2 -Wall -o /out/init init/init.c"
fi

# The ramdisk is init plus the empty mountpoints it needs. Everything else the
# system uses is mounted from the device's own /system at runtime, which is why
# no Amazon code is redistributed.
mkdir -p "$WORK/root"/{dev,proc,sys,system,data,etc,sbin}

# emOS's own wpa_supplicant, when one has been built. FireOS 6's cannot be
# used at all -- it is linked against Android IPC and aborts when /dev/binder
# is absent -- and ours (hostap 2.10, static ARM32, nl80211, internal crypto)
# needs nothing from Android. Optional on purpose: an image built without it
# falls back to /system/bin/wpa_supplicant, which is what the FireOS 5 fleet
# runs today. See tools/build-wpa-supplicant.sh.
SUPPLICANT=${SUPPLICANT:-$HERE/prebuilt/wpa_supplicant}
if [ -f "$SUPPLICANT" ]; then
    install -m 0755 "$SUPPLICANT" "$WORK/root/sbin/wpa_supplicant"
    echo "including wpa_supplicant ($(stat -c%s "$SUPPLICANT") bytes)"
fi

# wpa_cli and em-wifi, the console's way to set WiFi without the wizard. The
# console is the only channel left when the network is the broken thing, so
# these ride in the ramdisk rather than living on /data.
WPA_CLI=${WPA_CLI:-$HERE/prebuilt/wpa_cli}
if [ -f "$WPA_CLI" ]; then
    install -m 0755 "$WPA_CLI" "$WORK/root/sbin/wpa_cli"
    install -m 0755 "$HERE/device/em-wifi" "$WORK/root/sbin/em-wifi"
    echo "including wpa_cli and em-wifi"
fi

# Build identity, stamped in at build time rather than written at boot: it
# describes the IMAGE, so it must not be something a running system can drift
# from. /etc/os-release is the standard location and format, so ordinary
# tooling can read it without knowing anything about emOS. init's /etc symlink
# farm leaves it alone — symlink() into an existing name simply fails.
# emOS has its own tag namespace. Without --match, `git describe` picks up the
# nearest tag of ANY kind and stamps the image with a controller release
# number, which is worse than "unknown" because it looks plausible.
EMOS_VERSION=${EMOS_VERSION:-$(git -C "$HERE" describe --tags --match 'emos-v*' --dirty 2>/dev/null \
    || echo "0.1-$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)")}
cat > "$WORK/root/etc/os-release" <<OSREL
NAME="emOS"
ID=emos
PRETTY_NAME="emOS $EMOS_VERSION"
VERSION="$EMOS_VERSION"
VERSION_ID="$EMOS_VERSION"
BUILD_ID="$(date -u +%Y%m%dT%H%M%SZ)"
HOME_URL="https://github.com/wilbowes/EchoMuse"
OSREL
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
