#!/bin/bash
# Build wpa_supplicant and wpa_cli for emOS: static ARM32, nl80211, no external
# deps. Run inside the echomuse-compiler image; it fetches its own sources, or
# uses wpa_supplicant-2.10/ and libnl-tiny/ if they are already beside it.
# See emos-fireos6-system-as-root memory for why each workaround below is
# needed -- none of them are optional.
#
#   ./build-wpa-supplicant.sh [output dir]
#
# ARMV7A, and that is right for BOTH kernels, unlike the init beside it. These
# run as ordinary processes rather than as PID 1 under the kernel's own ABI, and
# a 64-bit kernel runs 32-bit binaries -- it has to, since the whole Android
# userspace on biscuit is 32-bit on top of FireOS 5's 64-bit kernel. Only the
# init needs two builds.
set -e
OUT=${1:-$(cd "$(dirname "$0")/.." && pwd)/prebuilt}
NDK=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin
CC=$NDK/armv7a-linux-androideabi21-clang
AR=$NDK/llvm-ar

# 0. The sources, PINNED. They were fetched by hand from `master` when these
#    binaries were first built, which is not reproducible: nobody can say what
#    the shipped binary was built from, and that is the thing an auditor of a
#    published artifact most needs to know.
#
#    hostap ships release tarballs, so that one is pinned by sha256 -- the value
#    below is Arch's published sha256sum for the same file, cross-checked rather
#    than taken from one download of ours.
#
#    libnl-tiny has no releases, so it is pinned by COMMIT and cloned rather
#    than fetched as a GitHub archive: codeload tarballs are generated, and their
#    bytes are not guaranteed stable across git versions, so a sha256 over one is
#    a pin that can fail for no reason. A commit id is a content hash already.
WPA_VER=2.10
WPA_SHA256=20df7ae5154b3830355f8ab4269123a87affdea59fe74fe9292a91d0d7e17b2f
LIBNL_REPO=https://github.com/openwrt/libnl-tiny.git
LIBNL_COMMIT=40493a655d8caa2ccf5206dde1e733abe2920432

if [ ! -d "wpa_supplicant-$WPA_VER" ]; then
    echo "fetching wpa_supplicant $WPA_VER"
    curl -sfL -o wpa.tar.gz "https://w1.fi/releases/wpa_supplicant-$WPA_VER.tar.gz"
    echo "$WPA_SHA256  wpa.tar.gz" | sha256sum -c - || {
        echo "wpa_supplicant tarball does not match its pin -- refusing" >&2
        exit 1
    }
    tar xzf wpa.tar.gz
fi
if [ ! -d libnl-tiny ]; then
    echo "fetching libnl-tiny $LIBNL_COMMIT"
    git clone -q "$LIBNL_REPO" libnl-tiny
    # Verified rather than trusted: `git checkout <sha>` fails on a repository
    # that does not contain that object, so the clone cannot substitute another.
    git -C libnl-tiny checkout -q "$LIBNL_COMMIT"
fi

# 1. libnl-tiny (full libnl will not cross-compile against bionic: its bundled
#    kernel headers collide, in_addr_t undefined then struct in_addr redefined).
#    Its private netlink structs collide too, so defer to <linux/netlink.h>.
cd libnl-tiny
python3 - <<'PY'
import re
p='include/netlink/netlink-kernel.h'
s=open(p).read()
if '#include <linux/netlink.h>' not in s:
    for n in ('sockaddr_nl','nlmsghdr','nlmsgerr','nl_pktinfo'):
        s=re.sub(r'\nstruct %s\s*\n\{.*?\n\};\n' % n, '\n', s, flags=re.S)
    open(p,'w').write('#include <linux/netlink.h>\n'+s)
PY
for f in attr cache cache_mngt error genl genl_ctrl genl_family genl_mngt handlers msg nl object socket; do
    $CC -Os -w -fPIC -I include -c $f.c -o $f.o
done
$AR rcs libnl-tiny.a ./*.o
cd ..

# 2. bionic has no librt; hostap links -lrt unconditionally.
mkdir -p /tmp/stub && : > /tmp/empty.c
$CC -c /tmp/empty.c -o /tmp/empty.o && $AR rcs /tmp/stub/librt.a /tmp/empty.o
cp libnl-tiny/libnl-tiny.a /tmp/stub/

# 4. A line this build cannot parse must not discard the WHOLE network block.
#    A device that has booted FireOS 6 comes back with Amazon's conf on /data,
#    carrying fields ours rejects (p2p_no_group_iface) and known fields with
#    out-of-range values (max_oper_chwidth=255) -- and one of either loses the
#    network entirely, which means no WiFi and no way to say so but a cable.
#    Upstream already does exactly this for unsupported WEP parameters a few
#    lines above; this extends it to anything else it cannot use. Structural
#    faults (missing '=', bad quoting, unterminated block) still count.
cd "wpa_supplicant-$WPA_VER"
python3 - <<'PYEOF'
p='wpa_supplicant/config_file.c'
s=open(p).read()
old = """#endif /* CONFIG_WEP */
			errors++;
		}"""
new = """#endif /* CONFIG_WEP */
			/* emOS: warn and carry on rather than discarding the
			 * network. See tools/build-wpa-supplicant.sh. */
			wpa_printf(MSG_WARNING,
				   "Line %d: ignoring unusable field '%s'",
				   *line, pos);
			continue;
		}"""
if 'emOS: warn and carry on' not in s:
    assert old in s, "config_file.c network parse site not found"
    s = s.replace(old, new, 1)
    # And the same for an unknown GLOBAL, which otherwise discards the entire
    # file rather than just the line -- p2p_no_group_iface is one FireOS 6
    # writes and this build does not know.
    gold = '''\t\t} else if (wpa_config_process_global(config, pos, line) < 0) {
\t\t\twpa_printf(MSG_ERROR, "Line %d: Invalid configuration "
\t\t\t\t   "line '%s'.", line, pos);
\t\t\terrors++;
\t\t\tcontinue;
\t\t}'''
    gnew = '''\t\t} else if (wpa_config_process_global(config, pos, line) < 0) {
\t\t\twpa_printf(MSG_WARNING, "Line %d: ignoring unusable line "
\t\t\t\t   "'%s'", line, pos);
\t\t\tcontinue;
\t\t}'''
    assert gold in s, "config_file.c global parse site not found"
    s = s.replace(gold, gnew, 1)
    open(p,'w').write(s)
    print("config_file.c patched (network + global)")
PYEOF
cd ..

# 3. hostap's priv_netlink.h redefines the same structs. The system includes
#    must come BEFORE its #ifndef IFLA_* fallbacks, or those become macros and
#    the kernel enums fail to parse.
cd "wpa_supplicant-$WPA_VER"
python3 - <<'PY'
import re
p='src/drivers/priv_netlink.h'
s=open(p).read()
if 'emOS' not in s:
    for n in ('sockaddr_nl','nlmsghdr','ifinfomsg','rtattr'):
        s=re.sub(r'\nstruct %s\s*\n\{.*?\n\};\n' % n, '\n/* emOS: from <linux/netlink.h> */\n', s, flags=re.S)
    s=s.replace('#define PRIV_NETLINK_H',
                '#define PRIV_NETLINK_H\n#include <linux/netlink.h>\n#include <linux/rtnetlink.h>',1)
    open(p,'w').write(s)
PY
cd wpa_supplicant
cat > .config <<'CFG'
CONFIG_DRIVER_NL80211=y
CONFIG_LIBNL_TINY=y
CONFIG_CRYPTO=internal
CONFIG_TLS=internal
CONFIG_INTERNAL_LIBTOMMATH=y
CONFIG_BACKEND=file
CONFIG_CTRL_IFACE=y
CONFIG_NO_RANDOM_POOL=y
CFG
export CC AR RANLIB=$NDK/llvm-ranlib
export CFLAGS="-Os -w -I$PWD/../../libnl-tiny/include"
export LDFLAGS="-static -L/tmp/stub"
make clean >/dev/null 2>&1 || true

# BOTH binaries. wpa_cli was built by hand when these were first made, so the
# script only ever produced half of what the image ships -- and the half it
# skipped is the one init needs to nudge the supplicant into associating.
#
# -j1 rather than $(nproc): the point of this script is now a PUBLISHED artifact,
# and a parallel make is one more thing that differs between the machine that
# built it and anyone checking it. It costs seconds on a codebase this size.
make -j1 wpa_supplicant wpa_cli

# Stripped, which is also what makes the output independent of where it was
# built: hostap has no __DATE__/__TIME__, but debug sections carry the absolute
# build path, and those go here.
$NDK/llvm-strip wpa_supplicant wpa_cli

mkdir -p "$OUT"
install -m 0755 wpa_supplicant wpa_cli "$OUT/"
echo
echo "built into $OUT:"
for f in wpa_supplicant wpa_cli; do
    printf '  %-16s %8d bytes  %s\n' "$f" "$(stat -c%s "$OUT/$f")" \
        "$(sha256sum "$OUT/$f" | cut -c1-16)…"
done
