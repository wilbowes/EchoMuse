"""
FireOS 5 to emOS over the network.

**Irreversible: this rewrites the boot partition of an Echo running FireOS. A
first emOS boot that fails before init runs needs TWRP and a cable.**

The verdicts are pure, like em_emos_update's: the shell transport gathers the
text and these decide. `run_migration` is the sequence, written against
em_emos_update's `io` (see "The migration" below).

WHY THERE IS NO WIFI WRITER. Every FireOS-native Echo is amonet 1 / FireOS 5
(the FireOS flow refuses amonet 2). A FireOS 5 emOS image ships no /sbin
userspace, so emOS runs Amazon's own /system/bin/wpa_supplicant, and init's
wpa_conf() falls back to Android's /data/misc/wifi/wpa_supplicant.conf when
/data/emos/wpa.conf does not exist. The same binary that wrote the conf reads
it, so FireOS's own fields (p2p_no_group_iface, last_connect_time, ...) are
safe there — unlike on FireOS 6, where emOS runs our 2.10 build and drops the
block. Measured on C95, 2026-11-10: no /data/emos/wpa.conf, Amazon's
supplicant running on Android's conf.

So the pre-flight only has to prove that Android's conf will take emOS back to
the network the Echo is on now:

- the Echo is associated, and its network has a block in the conf that the
  supplicant will use (not `disabled=1`);
- no BSS of that network is WPA-only. #1144 (a kernel panic joining a WPA-only
  AP) was seen on FireOS 6's kernel with our supplicant, not on FireOS 5's,
  but a refusal costs a user one message and a panic loop costs a cable.

A stale /data/emos/wpa.conf (an Echo that ran emOS before and went back) would
be preferred over Android's; the migration moves it aside, which is a write
and so not decided here.

wpa_supplicant's own grammar, hostap 2.x:
- wpa_cli prints SSIDs through printf_encode; unescape_ssid reverses it and
  mirrors device/internal/wifi/ssid.go UnescapeSSID, vector for vector.
- A conf `ssid=` is `"quoted"` (bytes up to the LAST quote, literal),
  `P"printf"` (printf_decode), or hex (wpa_config_parse_string).
"""
import hashlib
import re
import zlib

import em_emos_build as build
import em_emos_update as up

_HEX = re.compile(r"(?:[0-9a-fA-F]{2})+")
_SIMPLE = {"\\": 0x5C, '"': 0x22, "e": 0x1B, "n": 0x0A, "r": 0x0D, "t": 0x09}


def unescape_ssid(s: str) -> bytes:
    """wpa_cli's printf_encode output back to the SSID bytes. A malformed
    escape is kept literally, as the firmware's UnescapeSSID does."""
    raw = s.encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c != 0x5C or i + 1 >= len(raw):
            out.append(c)
            i += 1
            continue
        n = chr(raw[i + 1])
        if n in _SIMPLE:
            out.append(_SIMPLE[n])
            i += 2
        elif n == "x" and i + 3 < len(raw) and _HEX.fullmatch(
                raw[i + 2:i + 4].decode("latin-1")):
            out.append(int(raw[i + 2:i + 4], 16))
            i += 4
        else:
            out.append(c)
            i += 1
    return bytes(out)


def conf_ssid(value: str):
    """A conf `ssid=` value to bytes, or None when it is none of the three
    forms wpa_supplicant accepts."""
    if value.startswith('"'):
        end = value.rfind('"')
        if end <= 0:
            return None
        return value[1:end].encode("utf-8", "surrogateescape")
    if value.startswith('P"'):
        end = value.rfind('"')
        if end <= 1:
            return None
        return unescape_ssid(value[2:end])
    if _HEX.fullmatch(value):
        return bytes.fromhex(value)
    return None


def _lines(text: str):
    """Conf lines as wpa_config_get_line sees them: whitespace trimmed both
    ends, blank lines and `#` comments dropped. CR is whitespace, so a conf
    read through a pty parses the same."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def parse_conf_networks(text: str) -> list:
    """Each `network={...}` block: `ssid` (bytes, or None if unparseable),
    `disabled` (only `disabled=1` counts, as in hostap) and `key_mgmt`."""
    nets, cur = [], None
    for line in _lines(text):
        if cur is None:
            if line == "network={":
                cur = {"ssid": None, "disabled": False, "key_mgmt": None}
            continue
        if line == "}":
            nets.append(cur)
            cur = None
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "ssid":
            cur["ssid"] = conf_ssid(value)
        elif key == "disabled":
            cur["disabled"] = value == "1"
        elif key == "key_mgmt":
            cur["key_mgmt"] = value
    return nets


def parse_status(text: str) -> dict:
    """`wpa_cli status`: every `key=value`, with `ssid` as bytes."""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.rstrip("\r").partition("=")
        if sep:
            out[key] = value
    if "ssid" in out:
        out["ssid"] = unescape_ssid(out["ssid"])
    return out


def parse_scan(text: str) -> list:
    """`wpa_cli scan_results` rows as (flags, ssid bytes). Tab-separated; the
    SSID is the fifth column and may itself be empty."""
    rows = []
    for line in text.splitlines():
        cols = line.rstrip("\r").split("\t")
        if len(cols) < 4 or cols[0].startswith("bssid"):
            continue
        rows.append((cols[3], unescape_ssid(cols[4]) if len(cols) > 4 else b""))
    return rows


def wpa_only(flags: str) -> bool:
    """A BSS that offers WPA (v1) and no RSN: `[WPA-PSK-CCMP]`, never
    `[WPA-PSK-CCMP][WPA2-PSK-CCMP]`."""
    return "[WPA-" in flags and "[WPA2-" not in flags and "[RSN-" not in flags


def wifi_problems(conf_text: str, status_text: str, scan_text: str) -> list:
    """Why emOS might not get this Echo back on its network, or []."""
    st = parse_status(status_text)
    if st.get("wpa_state") != "COMPLETED" or not st.get("ssid"):
        return ["This Echo is not connected to a WiFi network right now "
                f"(state {st.get('wpa_state') or 'unknown'})."]
    ssid = st["ssid"]
    problems = []

    nets = parse_conf_networks(conf_text)
    matching = [n for n in nets if n["ssid"] == ssid]
    if not matching:
        problems.append(
            "FireOS's WiFi settings have no entry for the network this Echo "
            "is on, so emOS would not find it.")
    elif all(n["disabled"] for n in matching):
        problems.append(
            "FireOS's WiFi settings mark the network this Echo is on as "
            "disabled, so emOS would not join it.")

    if st.get("key_mgmt") == "WPA-PSK" or any(
            s == ssid and wpa_only(f) for f, s in parse_scan(scan_text)):
        problems.append(
            "An access point on this network offers only WPA (not WPA2). "
            "Switch it to WPA2 first (#1144).")
    return problems


# ── The migration ────────────────────────────────────────────────────────────
#
# em_emos_update's sequence with a FireOS image as the starting point. What is
# shared is reused, not copied: the chunked read, the trial mark, the write and
# read-back, the restore on a write that does not verify. What differs:
#
# - The reference is FireOS's (Magisk-patched) image, so it is proved to be the
#   RUNNING image by the kernel itself: /proc/version's banner must be inside
#   the image's decompressed kernel. The os-release check the update uses does
#   not exist on FireOS.
# - The cmdline changes (ramoops is added), so `migrated_problems` replaces
#   built_problems: everything else in the header and the kernel byte for byte.
# - Only amonet 1 / FireOS 5.1.1 booted from slot _a is accepted, by GPT name
#   and LK bytes, never by number alone (VVV, 2026-11-10: p10 boot_a_x,
#   p13 system_a, Android's bare boot_a is p17 = the exploit payload).
# - Leftovers of an earlier emOS life are cleared first: a boot.state of 3
#   would roll the first boot back at once, and a /data/emos/wpa.conf would be
#   preferred over Android's.
# - The FireOS image is escrowed on the controller before anything is written
#   (io.escrow): an image that fails before init needs TWRP, and this is the
#   image to put back with it.
# - FireOS restarts through its own init (`setprop sys.powerctl reboot`);
#   SIGTERM to PID 1 is emOS's path only.
#
# io is em_emos_update's, plus:
#   await io.escrow(data, md5) -> str    keep the FireOS image; "" ok, else why

FIREOS_RELEASE = "5.1.1"
BOOT_PARTNAME = "boot_a_x"
SYSTEM_PART = "mmcblk0p13"
SYSTEM_PARTNAME = "system_a"
LK_PART = "mmcblk0p3"
LK_PARTNAME = "lk_a"
# amonet 1's LK header: MediaTek magic, then its length (358,692), LE.
AMONET1_LK = "8816885824790500"
ANDROID_CONF = "/data/misc/wifi/wpa_supplicant.conf"
EMOS_CONF = "/data/emos/wpa.conf"
WPA_CLI = "wpa_cli -p /data/misc/wifi/sockets -i wlan0"
# Long enough for three failed trial boots, the restore and FireOS's own slow
# boot (163s measured after an unlock) to come back.
WATCH_S = up.WATCH_S + 300


def _partname(n: str, tag: str) -> str:
    """Echo `TAG:<GPT name>` of mmcblk0p<n>, with shell builtins only: FireOS
    5's shell has no grep, sed or head on the path."""
    return (f"while read -r l; do case \"$l\" in PARTNAME=*) "
            f"echo \"{tag}:${{l#PARTNAME=}}\";; esac; done "
            f"< /sys/class/block/{n}/uevent; ")


def preflight_cmd() -> str:
    """One round trip: what FireOS is running, and what emOS will find.

    The WiFi conf comes back without its secrets: lines carrying a key are
    dropped on the device, so a PSK never crosses the shell plane. An SSID
    that happens to contain `psk=` is dropped with them and the network is then
    reported missing — a refusal, never a wrong pass.
    """
    hdr = "busybox dd if={} bs={} count={} 2>/dev/null | busybox base64"
    return (
        "busybox mkdir -p /data/emos; "
        "echo \"REL:$(getprop ro.build.version.release)\"; "
        "echo \"SLOT:$(getprop ro.boot.slot_suffix)\"; "
        "echo BANNER_BEGIN; cat /proc/version; echo BANNER_END; "
        + _partname("mmcblk0p10", "BOOTNAME")
        + _partname(SYSTEM_PART, "SYSNAME")
        + _partname(LK_PART, "LKNAME")
        + f"echo LK_BEGIN; {hdr.format('/dev/block/' + LK_PART, 8, 1)}; echo LK_END; "
        f"echo HDR_BEGIN; {hdr.format(up.BOOT_DEV, up.BLOCK, 1)}; echo HDR_END; "
        f"echo \"SECTORS:$(cat /sys/class/block/mmcblk0p10/size 2>/dev/null)\"; "
        f"echo \"MD5TOOL:$(echo x | busybox md5sum 2>/dev/null)\"; "
        f"echo \"B64TOOL:$(echo x | busybox base64 2>/dev/null)\"; "
        f"busybox dd if=/dev/zero of={up.NEW_IMG}.probe bs={up.BLOCK} count=1 "
        f"conv=notrunc,fsync 2>/dev/null && echo DDTOOL:ok; "
        f"rm -f {up.NEW_IMG}.probe; "
        f"echo \"FREE $(busybox df -m /data 2>/dev/null | busybox tail -1)\"; "
        f"[ -e {EMOS_CONF} ] && echo EMOSCONF:present; "
        "echo CONF_BEGIN; while IFS= read -r l; do case \"$l\" in "
        "*psk=*|*password=*|*wep_key*|*private_key*) ;; *) echo \"$l\";; esac; "
        f"done < {ANDROID_CONF}; echo CONF_END; "
        f"echo STATUS_BEGIN; {WPA_CLI} status; echo STATUS_END; "
        f"echo SCAN_BEGIN; {WPA_CLI} scan_results; echo SCAN_END; "
        f"echo {up.OK}"
    )


def parse_preflight(out: str) -> dict:
    """The reply as a dict, or {} when the command did not finish."""
    out = (out or "").replace("\r", "")
    if up.OK not in out:
        return {}
    b = up._between
    sectors = up._line(out, "SECTORS:")
    return {
        "release": up._line(out, "REL:"),
        "slot": up._line(out, "SLOT:"),
        "banner": b(out, "BANNER_BEGIN", "BANNER_END").strip(),
        "boot_name": up._line(out, "BOOTNAME:"),
        "system_name": up._line(out, "SYSNAME:"),
        "lk_name": up._line(out, "LKNAME:"),
        "lk": up._b64(b(out, "LK_BEGIN", "LK_END")).hex(),
        "header": up._b64(b(out, "HDR_BEGIN", "HDR_END")),
        "partition_bytes": int(sectors) * 512 if sectors.isdigit() else 0,
        "md5_ok": up._MD5_PROBE in up._line(out, "MD5TOOL:"),
        "b64_ok": up._line(out, "B64TOOL:") == "eAo=",
        "dd_ok": up._line(out, "DDTOOL:") == "ok",
        "free_line": up._line(out, "FREE "),
        "emos_conf": up._line(out, "EMOSCONF:") == "present",
        "conf": b(out, "CONF_BEGIN", "CONF_END"),
        "status": b(out, "STATUS_BEGIN", "STATUS_END"),
        "scan": b(out, "SCAN_BEGIN", "SCAN_END"),
    }


def preflight_problems(pre: dict) -> list:
    """Why this Echo cannot be migrated, or []. Every one refuses."""
    if not pre:
        return ["the Echo did not answer the pre-flight check"]
    problems = []
    if pre["release"] != FIREOS_RELEASE:
        return [f"this Echo runs Android {pre['release'] or 'unknown'}; only "
                f"FireOS 5 ({FIREOS_RELEASE}) can be migrated over the network"]
    if pre["slot"] != "_a":
        problems.append(f"this Echo booted from slot {pre['slot'] or 'unknown'}"
                        f"; only slot A can be migrated (#973)")
    if pre["boot_name"] != BOOT_PARTNAME:
        problems.append(f"mmcblk0p10 is {pre['boot_name'] or 'unnamed'}, not "
                        f"{BOOT_PARTNAME}: an unfamiliar partition layout")
    if pre["system_name"] != SYSTEM_PARTNAME:
        problems.append(f"{SYSTEM_PART} is {pre['system_name'] or 'unnamed'}, "
                        f"not {SYSTEM_PARTNAME}")
    if pre["lk_name"] != LK_PARTNAME or pre["lk"] != AMONET1_LK:
        problems.append("the bootloader is not amonet 1's")
    if not pre["banner"].startswith("Linux version "):
        problems.append("the running kernel's version could not be read")
    if not pre["md5_ok"]:
        problems.append("busybox md5sum does not run, so nothing sent could "
                        "be verified")
    if not pre["dd_ok"]:
        problems.append("busybox dd cannot write the way the migration needs "
                        "(conv=notrunc,fsync)")
    if not pre["b64_ok"]:
        problems.append("busybox base64 does not run, so the boot image "
                        "cannot be read")
    elif up.image_length(pre["header"]) == 0:
        problems.append("the boot partition could not be read as a boot image")
    elif up.is_ours(pre["header"]):
        problems.append("the boot partition already holds an emOS image")
    elif pre["partition_bytes"] == 0:
        problems.append("the boot partition's size could not be read")
    problems += wifi_problems(pre["conf"], pre["status"], pre["scan"])
    return problems


def running_kernel(image: bytes, banner: str) -> bool:
    """Whether `image`'s kernel is the one running: /proc/version's line is
    the kernel's own linux_banner, compiled in, so it is in the decompressed
    Image byte for byte."""
    if not banner.startswith("Linux version "):
        return False
    try:
        zimage = build.split_reference(image)["zimage"]
        at = zimage.find(b"\x1f\x8b\x08")
        if at < 0:
            return False
        kernel = zlib.decompressobj(31).decompress(zimage[at:], 64 << 20)
    except Exception:
        return False
    return banner.encode() in kernel


def reference_problems(image: bytes, banner: str) -> list:
    """Why the FireOS image read off p10 cannot be built from, or []."""
    if not image or up.image_length(image[:48]) != len(image):
        return ["what was read off the boot partition is not one whole boot image"]
    if up.is_ours(image):
        return ["the boot partition already holds an emOS image"]
    problems = []
    if build.reference_kernel_arch(image) != build.ARCH_ARM64:
        problems.append("its kernel is not the 64-bit FireOS 5 kernel")
    if not running_kernel(image, banner):
        problems.append("its kernel is not the one running, so it is not the "
                        "image this Echo booted")
    return problems


def migrated_problems(reference: bytes, image: bytes, version: str,
                      partition_bytes: int) -> list:
    """Why a built image must not be written, or [].

    The update's built_problems with one change: the cmdline gains exactly the
    ramoops block (amonet 1 carries no emos.system= stamp). The kernel, its
    addresses and the rest of the header are what runs before init can roll
    anything back, so they must be the running image's byte for byte.
    """
    if up.image_length(image[:48]) != len(image):
        return ["the built image's header does not describe it"]
    problems = []
    if partition_bytes and len(image) > partition_bytes:
        problems.append(f"the image is {len(image):,} bytes and the boot "
                        f"partition holds {partition_bytes:,}")
    if up._regions(image)[0] != up._regions(reference)[0]:
        problems.append("the kernel differs from the one read off the Echo")
    if image[8:16] != reference[8:16] or image[20:64] != reference[20:64]:
        problems.append("the load addresses changed")
    want = reference[64:576].split(b"\0")[0].split() + \
        build.RAMOOPS_CMDLINE.encode().split()
    if image[64:576].split(b"\0")[0].split() != want:
        problems.append("the command line is not FireOS's plus ramoops")
    if up.stored_id(image) != up.computed_id(image):
        problems.append("the built image's id does not match its contents")
    if up.ramdisk_os_release(image).get("VERSION_ID") != version:
        problems.append(f"the built image does not identify as {version}")
    return problems


# Clears what an earlier emOS life left on /data. `boot.state` first: init
# counts from it, and a 3 there would restore the known-good on the first
# boot. A wpa.conf is renamed rather than deleted: it is somebody's network.
PREPARE_CMD = (
    f"rm -f {up.BOOT_STATE} {up.ROLLBACK_REC} {up.TRIAL_MARK} "
    f"{up.TRIAL_MARK}.tmp {up.NEW_IMG} {up.NEW_IMG}.part; "
    f"[ -e {EMOS_CONF} ] && mv {EMOS_CONF} {EMOS_CONF}.pre-migration; "
    f"sync; [ -e {up.BOOT_STATE} ] || [ -e {EMOS_CONF} ] || echo PREP:ok; "
    f"echo {up.OK}")

# Android's own restart: init stops services and syncs. FireOS's shell dies
# with the server, so OK is echoed first.
REBOOT_CMD = f"sync; echo {up.OK}; setprop sys.powerctl reboot"


async def run_migration(io) -> str:
    """Migrate the FireOS 5 Echo `io` is connected to. Returns the emOS
    version installed and confirmed; raises up.Refused otherwise."""
    U = up.UNCHANGED
    # 1. Is this an Echo the migration knows, and will emOS find its network?
    await io.step("Checking the Echo")
    pre = parse_preflight(await io.sh(preflight_cmd(), 60.0))
    problems = preflight_problems(pre)
    if problems:
        raise up.Refused("; ".join(problems) + U)
    length = up.image_length(pre["header"])
    why = up.space_problem(pre["free_line"], length)
    if why:
        raise up.Refused(why + U)

    # 2. Read the FireOS image and prove it is the one running.
    await io.step("Reading the FireOS boot image")
    reference = await up._read_image(io, length)
    if reference is None:
        raise up.Refused("could not read the boot partition off the Echo "
                         "intact" + U)
    problems = await io.offload(reference_problems, reference, pre["banner"])
    if problems:
        raise up.Refused("; ".join(problems) + U)
    ref_md5 = hashlib.md5(reference).hexdigest()

    # 3. The ways back: the controller's copy, then init's.
    why = await io.escrow(reference, ref_md5)
    if why:
        raise up.Refused(f"could not keep a copy of the FireOS image: {why}" + U)
    out = await io.sh(PREPARE_CMD, 30.0)
    if "PREP:ok" not in out:
        raise up.Refused("could not clear what an earlier emOS left on /data"
                         + U)
    out = await io.sh(up.refresh_good_cmd(length), 120.0)
    if up.md5_after(out, "DEVMD5:") != ref_md5:
        raise up.Refused("the boot partition changed while it was being read"
                         + U)
    if up.md5_after(out, "GOODMD5:") != ref_md5:
        raise up.Refused("could not save a verified copy of the FireOS image "
                         "to roll back to" + U)

    # 4. Build.
    init_bin, sbin, version = await io.payload(build.ARCH_ARM64)
    if not up._VERSION_SAFE.fullmatch(version or ""):
        raise up.Refused(f"{version!r} is not a usable version string" + U)
    if not up.init_supports_trial(init_bin):
        raise up.Refused(f"emOS {version}'s init cannot roll a failed boot "
                         f"back by itself, so it is not installed over the "
                         f"network" + U)
    await io.step(f"Building emOS {version} for this Echo's kernel")
    try:
        info = await io.offload(build.build_emos_image, reference, init_bin,
                                version, "", sbin, None,
                                getattr(io, "board", None))
    except build.BuildError as e:
        raise up.Refused(f"{e}" + U)
    image = info["image"]
    problems = await io.offload(migrated_problems, reference, image, version,
                                pre["partition_bytes"])
    if problems:
        raise up.Refused("; ".join(problems) + U)
    new_id = up.stored_id(image)
    new_build = up.ramdisk_os_release(image).get("BUILD_ID", "")

    # 5. Push, mark, write: em_emos_update's, unchanged.
    await io.step(f"Sending the image ({len(image):,} bytes)")
    why = await io.send(image, up.NEW_IMG)
    if why:
        await io.sh(up.CLEANUP_CMD, 20.0)
        raise up.Refused(f"sending the image failed: {why}" + U)
    if io.relink() != "same":
        await io.sh(up.CLEANUP_CMD, 20.0)
        raise up.Refused("the Echo reconnected during the transfer" + U
                         + "; try again")
    out = await io.sh(up.mark_cmd(new_id, new_build, version), 30.0)
    if up.OK not in out or up.parse_mark(
            up._between(out, "MARK_BEGIN", "MARK_END")).get("id") != new_id:
        await io.sh(up.CONFIRM_CMD, 20.0)
        raise up.Refused("could not write the trial mark" + U)

    await io.step("Writing the boot partition — do not unplug the Echo")
    verdict = up.write_verdict(
        await io.sh(up.write_cmd(up.NEW_IMG, len(image)), 180.0), info["md5"])
    if verdict != "ok":
        await io.sleep(10.0)
        out = await io.sh(up.verify_cmd(len(image)), 90.0)
        if up.md5_after(out, "DEVMD5:") == info["md5"]:
            verdict = "ok"
    if verdict != "ok":
        await up._restore(io, length, ref_md5)

    # 6. Restart into emOS and watch.
    await io.step(f"Restarting into emOS {version}")
    asked = io.now()
    await io.sh(REBOOT_CMD, 20.0)
    await _watch(io, version, new_build, asked)
    return version


async def _watch(io, version, new_build, asked):
    """After the restart: confirm emOS, or say what came back."""
    not_restarted = (f"the Echo did not restart. emOS {version} is written "
                     f"and verified, and will start the next time it restarts.")
    changed = False
    while io.now() < asked + WATCH_S:
        await io.sleep(3.0)
        link = io.relink()
        changed = changed or link != "same"
        if link == "gone":
            continue
        if not changed:
            if io.now() > asked + 120:
                raise up.Refused(not_restarted)
            continue
        st = up.parse_status(await io.sh(up.status_cmd(), 30.0))
        if not st:
            continue
        if st["version"]:
            await io.record(st["version"], st["build"])
        if st["build"] == new_build:
            if up.OK in await io.sh(up.CONFIRM_CMD, 30.0):
                return
            continue
        # FireOS on a link that only redialled: it has not restarted yet.
        if not st["version"] and up.booted_before(st["uptime"],
                                                  io.now() - asked):
            if io.now() > asked + 120:
                raise up.Refused(not_restarted)
            continue
        await io.sh(up.CONFIRM_CMD, 30.0)
        if not st["version"]:
            tries = st["rollback"].get("tries")
            if tries:
                raise up.Refused(
                    f"emOS {version} did not reach the controller within its "
                    f"trial, so the Echo put FireOS back by itself after "
                    f"{tries} unconfirmed boots. It is running FireOS as before.")
            raise up.Refused(
                "the Echo restarted into FireOS without starting emOS. Its "
                "boot partition reads back as emOS, so this needs looking at "
                "before trying again.")
        raise up.Refused(f"the Echo came back on emOS {st['version']} (build "
                         f"{st['build']}), which is not the image written")
    raise up.Refused(
        f"the Echo has not come back {WATCH_S // 60} minutes after "
        f"restarting. An amber ring means it is restoring FireOS; if it stays "
        f"off the network it needs TWRP and a USB cable, and the FireOS image "
        f"kept on the controller.")
