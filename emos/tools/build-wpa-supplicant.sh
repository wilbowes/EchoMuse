#!/bin/bash
# Build wpa_supplicant for emOS: static ARM32, nl80211, no external deps.
# Run from a directory holding wpa_supplicant-2.10/ and libnl-tiny-master/,
# inside the echomuse-compiler image. See emos-fireos6-system-as-root memory
# for why each workaround is needed -- none of them are optional.
set -e
NDK=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin
CC=$NDK/armv7a-linux-androideabi21-clang
AR=$NDK/llvm-ar

# 1. libnl-tiny (full libnl will not cross-compile against bionic: its bundled
#    kernel headers collide, in_addr_t undefined then struct in_addr redefined).
#    Its private netlink structs collide too, so defer to <linux/netlink.h>.
cd libnl-tiny-master
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
cp libnl-tiny-master/libnl-tiny.a /tmp/stub/

# 4. A line this build cannot parse must not discard the WHOLE network block.
#    A device that has booted FireOS 6 comes back with Amazon's conf on /data,
#    carrying fields ours rejects (p2p_no_group_iface) and known fields with
#    out-of-range values (max_oper_chwidth=255) -- and one of either loses the
#    network entirely, which means no WiFi and no way to say so but a cable.
#    Upstream already does exactly this for unsupported WEP parameters a few
#    lines above; this extends it to anything else it cannot use. Structural
#    faults (missing '=', bad quoting, unterminated block) still count.
cd /w/wpa_supplicant-2.10 2>/dev/null || cd "$(dirname "$0")/../../wpa_supplicant-2.10"
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
cd - >/dev/null

# 3. hostap's priv_netlink.h redefines the same structs. The system includes
#    must come BEFORE its #ifndef IFLA_* fallbacks, or those become macros and
#    the kernel enums fail to parse.
cd wpa_supplicant-2.10
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
export CFLAGS="-Os -w -I$PWD/../../libnl-tiny-master/include"
export LDFLAGS="-static -L/tmp/stub"
make clean >/dev/null 2>&1 || true
make -j"$(nproc)" wpa_supplicant
$NDK/llvm-strip wpa_supplicant
ls -l wpa_supplicant
