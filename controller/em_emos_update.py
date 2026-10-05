"""
Updating emOS on a device that is already running it, over the network (#573).

The image cannot be shipped — it carries the device's own kernel and DTBs — so
an update is a REBUILD: read the running image off the boot partition, keep its
kernel and cmdline byte for byte, swap the ramdisk for one built from the
release's init, and write it back. `em_emos_build` does the packing; this
module holds every decision around it, so they can be tested without a device
or aiohttp. `em_api._run_emos_update_locked` only carries.

amonet 1 and 2 need no separate path. The running image is on mmcblk0p10 on
both (emos/init/init.c hardcodes it for its own rollback), and everything that
differs between them — kernel architecture, the `emos.system=` stamp or its
absence — is already inside that image and is carried across untouched.

What can go wrong, and what answers each:

- **The read, the push or the write corrupts bytes.** md5 at both ends of each,
  and the write is read back after dropping the page cache. A write that does
  not read back is undone from `boot-good.img` while the old init still runs.
- **The partition does not hold the running image.** `reference_problems`
  compares the os-release inside the image read against the one the device
  reports, so a layout nobody has seen is refused rather than rebuilt from.
- **The new image boots and is no good.** The trial mark (`trial_mark`): init
  from 0.10 does not confirm an image the mark names until the controller
  removes it, reboots after three minutes unconfirmed, and restores the old
  image after three tries. emos/init/trialcheck.c pins init's half.
- **The new image fails before init runs.** Not recoverable from here — it
  needs TWRP and a cable, as the wizard's flash does. That is what the
  identical-kernel check and the read-back exist to make rare. A power cut
  during the ~1s write lands here too.
"""

import base64
import gzip
import hashlib
import re
import struct
import zlib

import em_emos_build as build
from em_oww_assets import parse_free_mb

BOOT_DEV = "/dev/block/mmcblk0p10"
OS_RELEASE = "/etc/os-release"
GOOD_IMG = "/data/emos/boot-good.img"
BOOT_STATE = "/data/emos/boot.state"
TRIAL_MARK = "/data/emos/update.pending"
NEW_IMG = "/data/emos/boot-new.img"
# Written by init when it restores the known-good image (from 0.10).
ROLLBACK_REC = "/data/emos/rollback.last"

# The first emOS whose init understands the trial mark. Anything older would
# be flashed with nothing to roll it back unattended, so it is never offered.
MIN_TARGET = (0, 10)

BLOCK = build.PAGE
# Blocks per read from the device: 512KB of image, ~700KB of base64.
CHUNK_BLOCKS = 256

# init's trial: TRIAL_SECS per boot, MAX_TRIES boots, then a restore and one
# more boot. The watch has to outlast all of it to report a rollback as one.
TRIAL_SECS = 180
MAX_TRIES = 3
WATCH_S = TRIAL_SECS * MAX_TRIES + 360

OK = "__EM_OTA_OK__"

_VERSION_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_HEX40 = re.compile(r"[0-9a-f]{40}")
# Used with fullmatch: `$` would also accept a trailing newline, and these
# values are written into shell commands.
_MD5 = re.compile(r"\b([0-9a-f]{32})\b")


# ── Versions ─────────────────────────────────────────────────────────────────

def version_key(text):
    """("emos-v0.9", "0.9", "emos-v0.9-3-gabc1234") -> (0, 9, 0, commits).

    None when it is not a version: the wizard stamps "0.1" for a hand-picked
    init and build.sh stamps `git describe`, so both forms are in the field.
    """
    t = (text or "").strip()
    t = t.removeprefix("emos-").lstrip("v")
    parts = t.split("-")
    nums = parts[0].split(".")
    if not 2 <= len(nums) <= 3 or not all(n.isdigit() for n in nums):
        return None
    nums = [int(n) for n in nums] + [0] * (3 - len(nums))
    commits = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return (*nums, commits)


def can_target(version) -> bool:
    """Whether an update may install `version` — see MIN_TARGET."""
    k = version_key(version)
    return k is not None and k[:2] >= MIN_TARGET


def update_available(running, latest) -> bool:
    """Whether `latest` should be offered to a device running `running`.

    An unknown running version is NOT offered an update: the firmware rule
    (any difference is an update) moves dev builds onto a release, and here
    the same guess costs a boot partition write.
    """
    r, l = version_key(running), version_key(latest)
    if r is None or l is None or not can_target(latest):
        return False
    return l > r


def offer(running, latest) -> str:
    """What the Updates tab says about `latest` for a device on `running`.

    "update"   offer it.
    "current"  running it, or ahead of it.
    "wizard"   newer, but its init predates network updates (MIN_TARGET), so
               "Up to date" would be untrue and an Update button a refusal.
    "unknown"  either version is missing or is not one.
    """
    r, l = version_key(running), version_key(latest)
    if r is None or l is None:
        return "unknown"
    if l <= r:
        return "current"
    return "update" if can_target(latest) else "wizard"


# ── os-release ───────────────────────────────────────────────────────────────

def parse_os_release(text) -> dict:
    """KEY=value lines, per os-release(5): optional quotes, # comments."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip()] = val
    return out


def running_emos(osr: dict):
    """(version, build) from a device's os-release, or (None, None) if the
    file is not emOS's — FireOS has none, and Amazon's is not ours."""
    if osr.get("ID") != "emos":
        return None, None
    return osr.get("VERSION_ID") or None, osr.get("BUILD_ID") or None


# ── The image ────────────────────────────────────────────────────────────────

def image_length(header: bytes) -> int:
    """Bytes of boot image on the partition, from its own header. 0 if it is
    not one. The same sum as init.c's boot_image_len."""
    if len(header) < 48 or header[:8] != b"ANDROID!":
        return 0
    ksz, _, rsz, _, ssz, _, _, psz = struct.unpack("<8I", header[8:40])
    if psz != BLOCK:
        return 0
    pad = lambda n: (n + BLOCK - 1) // BLOCK * BLOCK
    return BLOCK + pad(ksz) + pad(rsz) + pad(ssz)


def stored_id(image: bytes) -> str:
    """The 20-byte id in the header, as init's trial mark spells it."""
    return image[576:596].hex()


def _regions(image: bytes):
    ksz, _, rsz = struct.unpack("<3I", image[8:20])
    pad = lambda n: (n + BLOCK - 1) // BLOCK * BLOCK
    kernel = image[BLOCK:BLOCK + ksz]
    roff = BLOCK + pad(ksz)
    return kernel, image[roff:roff + rsz]


def computed_id(image: bytes) -> str:
    """The id `em_emos_build.pack` would give these regions."""
    kernel, ramdisk = _regions(image)
    digest = hashlib.sha1()
    for region in (kernel, ramdisk, b""):
        digest.update(region)
        digest.update(struct.pack("<I", len(region)))
    return digest.hexdigest()


def ramdisk_os_release(image: bytes) -> dict:
    """The os-release inside an image's ramdisk, or {} if there is none."""
    _, ramdisk = _regions(image)
    try:
        cpio = gzip.decompress(ramdisk)
    except (OSError, EOFError, zlib.error):
        return {}
    off = 0
    while off + 110 <= len(cpio) and cpio[off:off + 6] == b"070701":
        try:
            fields = [int(cpio[off + 6 + 8 * i:off + 14 + 8 * i], 16)
                      for i in range(13)]
        except ValueError:
            return {}
        size, namesize = fields[6], fields[11]
        name = cpio[off + 110:off + 110 + namesize].rstrip(b"\0")
        data_off = (off + 110 + namesize + 3) & ~3
        if name == b"TRAILER!!!":
            break
        if name == b"etc/os-release":
            return parse_os_release(
                cpio[data_off:data_off + size].decode(errors="replace"))
        off = (data_off + size + 3) & ~3
    return {}


def is_ours(image: bytes) -> bool:
    """The wizard's two markers: the stamp, and the ramoops block every image
    this packer has built carries (pre-0.5 images have no stamp)."""
    cmdline = image[64:576]
    return (build.SYSTEM_CMDLINE_KEY.encode() in cmdline
            or b"ramoops.mem_address=0x44400000" in cmdline)


def reference_problems(image: bytes, device_osr: dict) -> list:
    """Why the image read off the device cannot be rebuilt from, or [].

    Everything here refuses: this is the last check before bytes read off one
    device become the kernel written back to it.
    """
    if image_length(image[:48]) != len(image) or not image:
        return ["what was read off the boot partition is not one whole boot image"]
    if not is_ours(image):
        return ["the boot partition does not hold an emOS image"]
    problems = []
    # Our packer always writes a true id, so unlike a stock reference a
    # mismatch here is damage, not a tool that left a stale one.
    if stored_id(image) != computed_id(image):
        problems.append("the image's id does not match its contents")
    if not build.reference_kernel_arch(image):
        problems.append("its kernel architecture could not be read")
    inside = ramdisk_os_release(image)
    for key in ("VERSION_ID", "BUILD_ID"):
        if not inside.get(key) or inside.get(key) != device_osr.get(key):
            problems.append(
                f"the image on the boot partition is not the one running "
                f"({key} {inside.get(key)!r} against {device_osr.get(key)!r})")
            break
    return problems


def built_problems(reference: bytes, image: bytes, version: str,
                   partition_bytes: int) -> list:
    """Why a rebuilt image must not be written, or [].

    The kernel, the addresses and the cmdline are what an update must NOT
    change — a difference there is the packer misbehaving, and it is the part
    of the image that runs before init can roll anything back.
    """
    problems = []
    if image_length(image[:48]) != len(image):
        return ["the built image's header does not describe it"]
    if partition_bytes and len(image) > partition_bytes:
        problems.append(
            f"the image is {len(image):,} bytes and the boot partition holds "
            f"{partition_bytes:,}")
    if _regions(image)[0] != _regions(reference)[0]:
        problems.append("the kernel differs from the one read off the device")
    # Sizes aside (the ramdisk's changes), the header up to the id is the
    # addresses, the product name and the cmdline.
    if image[20:576] != reference[20:576] or image[8:16] != reference[8:16]:
        problems.append("the load addresses or the command line changed")
    if stored_id(image) != computed_id(image):
        problems.append("the built image's id does not match its contents")
    if stored_id(image) == stored_id(reference):
        problems.append("the built image is the one already installed")
    if ramdisk_os_release(image).get("VERSION_ID") != version:
        problems.append(f"the built image does not identify as {version}")
    return problems


def init_supports_trial(init_binary: bytes) -> bool:
    """Whether this init reads the trial mark — asked of the binary itself.

    The mark's path is a string constant in any init that does. A version
    comparison would say the same for a release and nothing for a local
    build, which is exactly what gets tested before a release exists.
    """
    return TRIAL_MARK.encode() in init_binary


# ── The trial mark ───────────────────────────────────────────────────────────

def trial_mark(image_id: str, build_id: str, version: str) -> str:
    """The mark's contents. Line one is init's; the rest are ours, for a
    controller that restarted mid-update (`mark_verdict`)."""
    if not _HEX40.fullmatch(image_id):
        raise ValueError(f"not an image id: {image_id!r}")
    if not _VERSION_SAFE.fullmatch(version) or not _VERSION_SAFE.fullmatch(build_id):
        raise ValueError("version or build id is not safe to write")
    return f"{image_id}\nbuild={build_id}\nversion={version}\n"


def parse_mark(text) -> dict:
    lines = (text or "").strip().splitlines()
    if not lines or not _HEX40.fullmatch(lines[0].strip()):
        return {}
    out = {"id": lines[0].strip()}
    for line in lines[1:]:
        key, sep, val = line.strip().partition("=")
        if sep and key in ("build", "version"):
            out[key] = val
    return out


def parse_rollback(text) -> dict:
    """init's rollback record: {"from", "to", "tries"}, or {} if there is
    none. An id init could not read arrives as "unknown" and is kept as that."""
    out = {}
    for line in (text or "").splitlines():
        key, sep, val = line.strip().partition("=")
        if sep and key in ("from", "to") and (
                _HEX40.fullmatch(val) or val == "unknown"):
            out[key] = val
        elif sep and key == "tries" and val.isdigit():
            out[key] = int(val)
    return out if {"from", "to", "tries"} <= out.keys() else {}


def rollback_text(rec: dict, running) -> str:
    """The sentence for the device log."""
    if rec["from"] != rec["to"]:
        return (f"emOS rolled back after {rec['tries']} unconfirmed boots: the "
                f"Echo restored its previous image and is running {running}")
    return (f"emOS rewrote its known-good image after {rec['tries']} "
            f"unconfirmed boots; the Echo is running {running}")


def mark_verdict(mark: dict, running_build) -> str:
    """What a mark found on a connected device means.

    "none"      no mark.
    "confirm"   it names the build that is running: the update worked.
    "rolled_back"  it names another: init restored the previous image (an init
                that predates the mark leaves it behind).
    """
    if not mark:
        return "none"
    if mark.get("build") and mark.get("build") == running_build:
        return "confirm"
    return "rolled_back"


# ── Commands ─────────────────────────────────────────────────────────────────
#
# Every value interpolated below is a constant from this file, an integer, or
# hex/version text that has passed a regex — nothing a user typed. Each command
# ends by echoing OK, and a reply without it is "did not run", never a pass.

def preflight_cmd() -> str:
    """One round trip: what is running, and whether it is safe to replace."""
    hdr = "busybox dd if={} bs={} count=1 2>/dev/null | busybox base64"
    return (
        f"echo OSR_BEGIN; cat {OS_RELEASE} 2>/dev/null; echo OSR_END; "
        f"echo \"STATE:$(cat {BOOT_STATE} 2>/dev/null)\"; "
        f"echo HDR_BEGIN; {hdr.format(BOOT_DEV, BLOCK)}; echo HDR_END; "
        f"echo GOOD_BEGIN; {hdr.format(GOOD_IMG, BLOCK)}; echo GOOD_END; "
        f"echo \"SECTORS:$(cat /sys/class/block/mmcblk0p10/size 2>/dev/null)\"; "
        f"echo \"MD5TOOL:$(echo x | busybox md5sum 2>/dev/null)\"; "
        f"echo \"B64TOOL:$(echo x | busybox base64 2>/dev/null)\"; "
        # The exact flags the write uses, against a scratch file.
        f"busybox dd if=/dev/zero of={NEW_IMG}.probe bs={BLOCK} count=1 "
        f"conv=notrunc,fsync 2>/dev/null && echo DDTOOL:ok; "
        f"rm -f {NEW_IMG}.probe; "
        f"echo \"FREE $(busybox df -m /data 2>/dev/null | busybox tail -1)\"; "
        f"echo MARK_BEGIN; cat {TRIAL_MARK} 2>/dev/null; echo MARK_END; "
        f"echo {OK}"
    )


def _between(out: str, a: str, b: str) -> str:
    if a not in out or b not in out:
        return ""
    return out.split(a, 1)[1].split(b, 1)[0]


def _line(out: str, prefix: str) -> str:
    for ln in out.splitlines():
        if ln.startswith(prefix):
            return ln[len(prefix):].strip()
    return ""


def _b64(text: str) -> bytes:
    try:
        return base64.b64decode("".join(text.split()), validate=True)
    except ValueError:
        return b""


# What `echo x | md5sum` must print, so a tool that runs and answers wrong is
# caught along with one that does not run.
_MD5_PROBE = hashlib.md5(b"x\n").hexdigest()


def parse_preflight(out: str) -> dict:
    """The preflight reply as a dict, or {} when the command did not finish."""
    out = out or ""
    if OK not in out:
        return {}
    sectors = _line(out, "SECTORS:")
    return {
        "os_release": parse_os_release(_between(out, "OSR_BEGIN", "OSR_END")),
        "state": _line(out, "STATE:"),
        "header": _b64(_between(out, "HDR_BEGIN", "HDR_END")),
        "good_header": _b64(_between(out, "GOOD_BEGIN", "GOOD_END")),
        "partition_bytes": int(sectors) * 512 if sectors.isdigit() else 0,
        "md5_ok": _MD5_PROBE in _line(out, "MD5TOOL:"),
        "b64_ok": _line(out, "B64TOOL:") == "eAo=",
        "dd_ok": _line(out, "DDTOOL:") == "ok",
        "free_line": _line(out, "FREE "),
        "mark": parse_mark(_between(out, "MARK_BEGIN", "MARK_END")),
    }


def preflight_problems(pre: dict) -> list:
    """Why this device cannot be updated right now, or []."""
    if not pre:
        return ["the device did not answer the preflight check"]
    version, _ = running_emos(pre["os_release"])
    if not version:
        return ["this device is not running emOS"]
    problems = []
    if not pre["md5_ok"]:
        problems.append("busybox md5sum does not run on this device, so "
                        "nothing sent to it could be verified")
    if not pre["dd_ok"]:
        problems.append("busybox dd on this device cannot write the way the "
                        "update needs (conv=notrunc,fsync)")
    if not pre["b64_ok"]:
        problems.append("busybox base64 does not run on this device, so the "
                        "running image cannot be read off it")
    elif image_length(pre["header"]) == 0:
        problems.append("the boot partition could not be read as a boot image")
    elif pre["partition_bytes"] == 0:
        problems.append("the boot partition's size could not be read")
    # Anything but a confirmed boot means init is still deciding about the
    # image that is running; replacing it now would hide that answer.
    if pre["state"] != "0":
        problems.append(
            f"the current boot is not confirmed (boot.state "
            f"{pre['state'] or 'unreadable'!r}) — let it settle and retry")
    return problems


def space_problem(free_line: str, length: int) -> str:
    """Why /data cannot take the update, or "". The new image lands beside its
    .part, and boot-good.img may be rewritten. An unreadable df is not a full
    disk, as in the firmware update."""
    free_mb = parse_free_mb(free_line)
    need_mb = (length * 3) // 1048576 + 8
    if free_mb is not None and free_mb < need_mb:
        return (f"not enough space on /data: {free_mb}MB free, needs "
                f"~{need_mb}MB")
    return ""


def good_matches(pre: dict) -> bool:
    """Whether boot-good.img is the running image, by header id."""
    h, g = pre.get("header", b""), pre.get("good_header", b"")
    return len(g) >= 596 and g[576:596] == h[576:596]


def chunks(length: int):
    """(skip, count) in blocks, covering an image of `length` bytes."""
    total = length // BLOCK
    return [(s, min(CHUNK_BLOCKS, total - s))
            for s in range(0, total, CHUNK_BLOCKS)]


def read_chunk_cmd(skip: int, count: int) -> str:
    dd = (f"busybox dd if={BOOT_DEV} bs={BLOCK} skip={int(skip)} "
          f"count={int(count)} 2>/dev/null")
    return (f"echo CHUNK_BEGIN; {dd} | busybox base64; echo CHUNK_END; "
            f"echo \"CHUNKMD5:$({dd} | busybox md5sum)\"; echo {OK}")


def parse_chunk(out: str, count: int):
    """The chunk's bytes, or None if it did not arrive whole and verified."""
    out = out or ""
    if OK not in out:
        return None
    data = _b64(_between(out, "CHUNK_BEGIN", "CHUNK_END"))
    if len(data) != count * BLOCK:
        return None
    return data if hashlib.md5(data).hexdigest() == md5_after(out, "CHUNKMD5:") else None


def _md5_of_device(length: int) -> str:
    return (f"busybox dd if={BOOT_DEV} bs={BLOCK} count={length // BLOCK} "
            f"2>/dev/null | busybox md5sum")


def verify_cmd(length: int) -> str:
    """Both digests, read off the flash rather than the page cache."""
    return "sync; echo 3 > /proc/sys/vm/drop_caches; " + digests_cmd(length)


def refresh_good_cmd(length: int) -> str:
    """Make boot-good.img the running image, and report both digests."""
    return (
        f"busybox dd if={BOOT_DEV} of={GOOD_IMG}.new bs={BLOCK} "
        f"count={length // BLOCK} 2>/dev/null && sync && "
        f"mv {GOOD_IMG}.new {GOOD_IMG}; " + digests_cmd(length))


def digests_cmd(length: int) -> str:
    return (f"echo \"DEVMD5:$({_md5_of_device(length)})\"; "
            f"echo \"GOODMD5:$(busybox md5sum {GOOD_IMG} 2>/dev/null)\"; "
            f"echo {OK}")


def mark_cmd(image_id: str, build_id: str, version: str) -> str:
    body = trial_mark(image_id, build_id, version)
    args = " ".join(f"'{ln}'" for ln in body.splitlines())
    return (f"printf '%s\\n' {args} > {TRIAL_MARK}.tmp && sync && "
            f"mv {TRIAL_MARK}.tmp {TRIAL_MARK} && sync && "
            f"echo MARK_BEGIN; cat {TRIAL_MARK}; echo MARK_END; echo {OK}")


def write_cmd(source: str, length: int) -> str:
    """Write `source` to the boot partition and read it back off the flash.

    The read-back follows a cache drop: a dd here can finish at page-cache
    speed, verify against that same cache, and be lost at the next reboot.
    """
    if source not in (NEW_IMG, GOOD_IMG):
        raise ValueError(f"not an image this module writes: {source!r}")
    return (
        f"busybox dd if={source} of={BOOT_DEV} bs={BLOCK} conv=notrunc,fsync "
        f"2>/dev/null; echo \"DD:$?\"; sync; "
        f"echo 3 > /proc/sys/vm/drop_caches; "
        f"echo \"READBACK:$({_md5_of_device(length)})\"; echo {OK}")


def md5_after(out: str, prefix: str) -> str:
    """The md5 on the line starting `prefix`, or ""."""
    for ln in (out or "").splitlines():
        if ln.startswith(prefix):
            m = _MD5.search(ln)
            return m.group(1) if m else ""
    return ""


def write_verdict(out: str, want_md5: str) -> str:
    """"ok", "mismatch" (the partition holds something else now) or
    "unknown" (the command did not finish, so nothing is known)."""
    if OK not in (out or ""):
        return "unknown"
    return "ok" if md5_after(out, "READBACK:") == want_md5 else "mismatch"


# init's SIGTERM path: stop services, sync, remount /data read-only, reboot.
REBOOT_CMD = f"sync; kill -TERM 1; echo {OK}"

CLEANUP_CMD = f"rm -f {NEW_IMG} {NEW_IMG}.part {TRIAL_MARK}.tmp; echo {OK}"
# Also removes init's rollback record: by the time this runs, whoever sent it
# has read the status and reported what the record said.
CONFIRM_CMD = f"rm -f {TRIAL_MARK} {ROLLBACK_REC}; sync; " + CLEANUP_CMD


def status_cmd() -> str:
    """What a connected emOS device is running, and any mark it carries."""
    return (f"echo OSR_BEGIN; cat {OS_RELEASE} 2>/dev/null; echo OSR_END; "
            f"echo MARK_BEGIN; cat {TRIAL_MARK} 2>/dev/null; echo MARK_END; "
            f"echo \"STATE:$(cat {BOOT_STATE} 2>/dev/null)\"; "
            f"echo \"UPTIME:$(cat /proc/uptime 2>/dev/null)\"; "
            f"[ -e {NEW_IMG} ] && echo NEWIMG:left; "
            f"echo RB_BEGIN; cat {ROLLBACK_REC} 2>/dev/null; echo RB_END; "
            f"echo {OK}")


def booted_before(uptime, since_s: float) -> bool:
    """Whether a kernel up for `uptime` seconds was already running `since_s`
    seconds ago. Unknown uptime is False: it cannot show the device stayed up."""
    return uptime is not None and uptime > since_s + 20


def parse_status(out: str) -> dict:
    out = out or ""
    if OK not in out:
        return {}
    version, build_id = running_emos(
        parse_os_release(_between(out, "OSR_BEGIN", "OSR_END")))
    return {"version": version, "build": build_id,
            "mark": parse_mark(_between(out, "MARK_BEGIN", "MARK_END")),
            "state": _line(out, "STATE:"),
            "uptime": _uptime(_line(out, "UPTIME:")),
            "leftover": _line(out, "NEWIMG:") == "left",
            "rollback": parse_rollback(_between(out, "RB_BEGIN", "RB_END"))}


def _uptime(text: str):
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return None


# ── The update itself ────────────────────────────────────────────────────────
#
# Written against `io`, the handful of things only the controller can do, so
# the whole sequence runs in a test against a simulated device with a real
# shell (tests/test_emos_update_flow.py). em_api._EmosIO is the real one.
#
#   await io.sh(cmd, timeout) -> str    run on the device; "" if it could not
#   await io.send(data, dest) -> str    verified file push; "" ok, else why
#   await io.payload(arch)              -> (init, sbin, version), or Refused
#   await io.offload(fn, *args)         run a blocking function off the loop
#   await io.step(msg, log=True)        progress for the panel and the log
#   await io.record(version, build)     what the device is running
#   await io.sleep(seconds); io.now()
#   io.relink() -> "same" | "gone" | "new"
#       whether the device is still on the connection `sh` talks to. After
#       "new", `sh` talks to the new one.

class Refused(Exception):
    """The update stopped. str() is what the operator reads."""


UNCHANGED = " — nothing was changed"


async def _read_image(io, length: int):
    parts = []
    plan = chunks(length)
    for n, (skip, count) in enumerate(plan, 1):
        for _ in range(3):
            data = parse_chunk(await io.sh(read_chunk_cmd(skip, count), 90.0), count)
            if data is not None or io.relink() != "same":
                break
        if data is None:
            return None
        parts.append(data)
        await io.step(f"Reading the running image ({n}/{len(plan)})", log=False)
    return b"".join(parts)


async def run_update(io) -> str:
    """Update the device `io` is connected to. Returns the version installed
    and confirmed; raises Refused with the reason otherwise."""
    # 1. Is it safe to replace what is running?
    await io.step("Checking the device")
    pre = parse_preflight(await io.sh(preflight_cmd(), 45.0))
    problems = preflight_problems(pre)
    if problems:
        raise Refused("; ".join(problems) + UNCHANGED)
    old_version, old_build = running_emos(pre["os_release"])
    await io.record(old_version, old_build)
    length = image_length(pre["header"])
    why = space_problem(pre["free_line"], length)
    if why:
        raise Refused(why + UNCHANGED)

    # 2. Read the running image, and prove it is the running image.
    await io.step(f"Reading the running image (emOS {old_version})")
    reference = await _read_image(io, length)
    if reference is None:
        raise Refused("could not read the boot partition off the device "
                      "intact" + UNCHANGED)
    problems = await io.offload(reference_problems, reference, pre["os_release"])
    if problems:
        raise Refused("; ".join(problems) + UNCHANGED)
    ref_md5 = hashlib.md5(reference).hexdigest()

    # 3. The way back: boot-good.img must BE the running image.
    out = await io.sh(digests_cmd(length) if good_matches(pre)
                      else refresh_good_cmd(length), 90.0)
    if md5_after(out, "DEVMD5:") != ref_md5:
        raise Refused("the boot partition changed while it was being read"
                      + UNCHANGED)
    if md5_after(out, "GOODMD5:") != ref_md5:
        out = await io.sh(refresh_good_cmd(length), 90.0)
        if md5_after(out, "GOODMD5:") != ref_md5:
            raise Refused("could not save a verified copy of the running "
                          "image to roll back to" + UNCHANGED)

    # 4. Build.
    arch = build.reference_kernel_arch(reference)
    init_bin, sbin, version = await io.payload(arch)
    if not _VERSION_SAFE.fullmatch(version or ""):
        raise Refused(f"{version!r} is not a usable version string" + UNCHANGED)
    if not init_supports_trial(init_bin):
        raise Refused(
            f"emOS {version}'s init cannot roll a failed update back by "
            f"itself, so it is not installed over the network" + UNCHANGED)
    await io.step(f"Building emOS {version} for this device's {arch} kernel")
    try:
        info = await io.offload(build.build_emos_image, reference, init_bin,
                                version, "", sbin, None)
    except build.BuildError as e:
        raise Refused(f"{e}" + UNCHANGED)
    image = info["image"]
    problems = await io.offload(built_problems, reference, image, version,
                                pre["partition_bytes"])
    if problems:
        raise Refused("; ".join(problems) + UNCHANGED)
    new_id = stored_id(image)
    new_build = ramdisk_os_release(image).get("BUILD_ID", "")

    # 5. Push, verified, to /data.
    await io.step(f"Sending the image ({len(image):,} bytes)")
    why = await io.send(image, NEW_IMG)
    if why:
        await io.sh(CLEANUP_CMD, 20.0)
        raise Refused(f"sending the image failed: {why}" + UNCHANGED)
    # A device that dropped and redialled meanwhile is a different boot or
    # link, and every check above was made against the old one.
    if io.relink() != "same":
        await io.sh(CLEANUP_CMD, 20.0)
        raise Refused("the device reconnected during the transfer"
                      + UNCHANGED + "; try again")

    # 6. Mark the trial, then write.
    out = await io.sh(mark_cmd(new_id, new_build, version), 30.0)
    if OK not in out or parse_mark(
            _between(out, "MARK_BEGIN", "MARK_END")).get("id") != new_id:
        await io.sh(CONFIRM_CMD, 20.0)
        raise Refused("could not write the trial mark" + UNCHANGED)

    await io.step("Writing the boot partition — do not unplug the Echo")
    verdict = write_verdict(
        await io.sh(write_cmd(NEW_IMG, len(image)), 180.0), info["md5"])
    if verdict != "ok":
        # Unknown (the link dropped mid-command) or a mismatch: ask the flash
        # itself, once any dd still running has had time to finish.
        await io.sleep(10.0)
        out = await io.sh(verify_cmd(len(image)), 90.0)
        if md5_after(out, "DEVMD5:") == info["md5"]:
            verdict = "ok"
    if verdict != "ok":
        await _restore(io, length, ref_md5)

    # 7. Restart into it and watch.
    await io.step(f"Restarting into emOS {version}")
    asked = io.now()
    # Waited on rather than fired and dropped: closing the session kills the
    # shell, and a `sync` still running would take the restart with it.
    await io.sh(REBOOT_CMD, 20.0)
    await _watch(io, version, new_build, old_version, old_build, asked)
    return version


async def _restore(io, length: int, ref_md5: str):
    """The write did not verify: put the running image back, while the init
    that is running it still is. Always raises."""
    for _ in range(5):
        if write_verdict(await io.sh(write_cmd(GOOD_IMG, length), 180.0),
                         ref_md5) == "ok":
            await io.sh(CONFIRM_CMD, 20.0)
            raise Refused(
                "the new image did not read back correctly after writing, so "
                "the previous image was written back and verified. The device "
                "is unchanged and safe to restart.")
        await io.sleep(5.0)
    # Irreversible if mishandled, so the whole instruction stays in the text.
    raise Refused(
        "DO NOT RESTART OR UNPLUG THIS ECHO. The new image did not read back "
        "correctly, and writing the previous image back did not verify "
        "either, so its boot partition may not hold a bootable image. It "
        "keeps working until it restarts. From its Console tab, run: "
        f"busybox dd if={GOOD_IMG} of={BOOT_DEV} bs={BLOCK} conv=notrunc,fsync "
        "— then compare `busybox md5sum` of both. If it restarts before "
        "that succeeds it will need TWRP, a USB cable and the provisioning "
        "wizard to recover.")


async def _watch(io, version, new_build, old_version, old_build, asked):
    """After the restart: confirm the new image, or say what came back."""
    not_restarted = (
        f"the Echo did not restart. emOS {version} is written and verified, "
        f"and will be used the next time it restarts.")
    # relink() says "new" once and "same" after it, so remember that the
    # connection changed: from then on "same" is the connection to ask.
    changed = False
    while io.now() < asked + WATCH_S:
        await io.sleep(3.0)
        link = io.relink()
        changed = changed or link != "same"
        if link == "gone":
            continue
        if not changed:
            if io.now() > asked + 90:
                raise Refused(not_restarted)
            continue
        st = parse_status(await io.sh(status_cmd(), 30.0))
        if not st:
            continue                    # the shell plane is not up yet
        await io.record(st["version"], st["build"])
        if st["build"] == new_build:
            if OK in await io.sh(CONFIRM_CMD, 30.0):
                return
            continue                    # confirm again on the next pass
        # The old image on a link that merely redialled is not a rollback: the
        # kernel has been up since before the restart was asked for. The mark
        # stays, so the trial still applies whenever it does restart.
        if st["build"] == old_build and booted_before(
                st["uptime"], io.now() - asked):
            if io.now() > asked + 90:
                raise Refused(not_restarted)
            continue
        await io.sh(CONFIRM_CMD, 30.0)
        if st["build"] == old_build:
            tries = st["rollback"].get("tries")
            raise Refused(
                f"rolled back to emOS {old_version}: the new image did not "
                f"reach the controller within its trial, so the Echo restored "
                f"the previous one by itself"
                + (f" after {tries} unconfirmed boots" if tries else ""))
        raise Refused(f"the Echo came back on emOS {st['version']} (build "
                      f"{st['build']}), which is neither the old image nor "
                      f"the new one")
    raise Refused(
        f"the Echo has not come back {WATCH_S // 60} minutes after "
        f"restarting. An amber ring means it is restoring the previous image; "
        f"if it stays off the network it needs its USB console.")


async def settle_on_connect(sh):
    """What a freshly connected emOS device runs, and any trial mark settled.

    For when no update task is watching — the controller restarted mid-update.
    Stateless for that reason: the mark carries the build it was written for,
    so "the device runs that build and reached us" is decidable from the
    device alone. Returns (status, verdict); status is {} if it did not answer.
    Verdicts are mark_verdict's, plus "incomplete": no mark, but the pushed
    image is still on /data, so an update was started and did not finish.
    """
    st = parse_status(await sh(status_cmd(), 30.0))
    if not st:
        return {}, "none"
    verdict = mark_verdict(st["mark"], st["build"])
    # init removes the mark when it restores the old image, so a rollback
    # nobody watched leaves no mark — only the image that was pushed. Found on
    # C95, 2026-10-02: rolled back with the controller stopped, 7MB left on
    # /data and nothing to say an update had been tried.
    if verdict == "none" and st["leftover"]:
        verdict = "incomplete"
    # init from 0.10 says so itself, which also covers a rollback that was
    # not an update at all.
    if st["rollback"]:
        verdict = "rolled_back"
    if verdict != "none" and OK not in await sh(CONFIRM_CMD, 30.0):
        verdict = "none"                # ask again on the next connect
    return st, verdict
