"""
The whole emOS update, run against a simulated device with a REAL shell.

em_emos_update.run_update is written against a small `io`, so the sequence —
preflight, read, build, push, mark, write, read back, restart, watch — runs
here without aiohttp or hardware. The device is a directory: its boot
partition, /data/emos and os-release are files, and every command the update
sends is executed by `sh` with the host's busybox after its paths are
remapped. So the commands themselves are under test, not a description of
them: a quoting mistake or a wrong dd argument fails here.

The simulated init follows emos/init/init.c's rollback and trial rules as
written there; trialcheck.c is what holds the real one to them.
"""

import asyncio
import os
import shutil
import subprocess

import pytest

import em_emos_build as eb
import em_emos_update as up
import test_emos_build as fx
from test_emos_update import installed, new_init

if shutil.which("busybox") is None:
    if os.environ.get("CI"):
        raise RuntimeError("busybox is required under CI: these tests are "
                           "what checks the commands written to a device")
    pytestmark = pytest.mark.skip(reason="needs busybox on the host")

REAL_BUSYBOX = shutil.which("busybox")

# Ubuntu's busybox is built without the base64 applet; the device's has it
# (the firmware OTA has always decoded with it). Everything else is the real
# thing.
SHIM = """#!/bin/sh
if [ "$1" = base64 ]; then shift; exec base64 "$@"; fi
exec {real} "$@"
"""

PART_BYTES = 1 << 20          # a small "partition"; the images are ~20KB
TAIL = b"\xEE" * 4096         # what lies past the image, which must survive


class Device:
    """A directory standing in for an Echo on emOS."""

    def __init__(self, root, image, good="same", state="0"):
        self.root = root
        (root / "data" / "emos").mkdir(parents=True)
        self.boot = root / "boot"
        self.good = root / "data" / "emos" / "boot-good.img"
        self.mark = root / "data" / "emos" / "update.pending"
        self.statef = root / "data" / "emos" / "boot.state"
        self.boot.write_bytes(image + TAIL + b"\0" * (PART_BYTES - len(image) - len(TAIL)))
        (root / "size").write_text(f"{PART_BYTES // 512}\n")
        if good == "same":
            self.good.write_bytes(image)
        elif good is not None:
            self.good.write_bytes(good)
        self.statef.write_text(state + "\n")
        (root / "bin").mkdir()
        shim = root / "bin" / "busybox"
        shim.write_text(SHIM.format(real=REAL_BUSYBOX))
        shim.chmod(0o755)
        self.uptime = 5000.0
        self.new_image_works = True
        self.ignore_reboot = False
        self.corrupt_writes = 0       # how many writes to spoil before read-back
        self.writes_record = True     # False: an init older than 0.10
        self.boots = 0
        self.trial = False
        self._load()

    # ── what init does ──
    def image(self):
        data = self.boot.read_bytes()
        return data[:up.image_length(data[:48])]

    def _load(self):
        osr = up.ramdisk_os_release(self.image())
        (self.root / "os-release").write_text(
            "".join(f'{k}="{v}"\n' for k, v in osr.items()))

    def _confirm(self):
        self.statef.write_text("0\n")
        self.good.write_bytes(self.image())

    def reboot(self):
        """Boot until something stays up, as init.c would. Returns how long
        that took: a failed trial costs TRIAL_SECS before init restarts."""
        elapsed = 0.0
        while True:
            self.boots += 1
            self.uptime = 30.0
            elapsed += 30.0
            tries = int(self.statef.read_text() or 0)
            if tries >= up.MAX_TRIES and self.good.exists():
                good = self.good.read_bytes()
                if self.writes_record:
                    (self.root / "data/emos/rollback.last").write_text(
                        f"from={up.stored_id(self.image())}\n"
                        f"to={up.stored_id(good)}\ntries={tries}\n")
                part = bytearray(self.boot.read_bytes())
                part[:len(good)] = good
                self.boot.write_bytes(bytes(part))
                self.mark.unlink(missing_ok=True)
                self.statef.write_text("0\n")
                continue
            self.statef.write_text(f"{tries + 1}\n")
            self._load()
            self.trial = False
            if self.mark.exists():
                first = self.mark.read_text().splitlines()[0:1]
                if first == [up.stored_id(self.image())]:
                    self.trial = True
                else:
                    self.mark.unlink()
            if not self.trial:
                self._confirm()                 # network-up confirms
                return elapsed
            if self.new_image_works:
                return elapsed                  # up, waiting for the controller
            elapsed += up.TRIAL_SECS            # unconfirmed: init restarts

    def tick(self):
        if (self.root / "reboot").exists():
            (self.root / "reboot").unlink()
            if not self.ignore_reboot:
                return self.reboot()
        if self.trial and not self.mark.exists():
            self.trial = False
            self._confirm()                     # the controller removed the mark
        return 0.0

    # ── the shell ──
    def run(self, cmd):
        r = self.root
        if self.corrupt_writes and f"of={up.BOOT_DEV}" in cmd:
            self.corrupt_writes -= 1
            cmd = cmd.replace(
                "echo 3 > /proc/sys/vm/drop_caches",
                f"printf X | busybox dd of={r}/boot bs=1 seek=5000 conv=notrunc 2>/dev/null")
        for a, b in (
            ("/sys/class/block/mmcblk0p10/size", f"{r}/size"),
            (up.BOOT_DEV, f"{r}/boot"),
            ("/proc/sys/vm/drop_caches", f"{r}/drop_caches"),
            ("cat /proc/uptime", f"echo {self.uptime}"),
            (up.OS_RELEASE, f"{r}/os-release"),
            ("/data/emos/", f"{r}/data/emos/"),
            ("df -m /data", f"df -m {r}/data"),
            ("kill -TERM 1", f"touch {r}/reboot"),
        ):
            cmd = cmd.replace(a, b)
        out = subprocess.run(
            ["sh", "-c", cmd], capture_output=True, text=True, timeout=60,
            env={"PATH": f"{r}/bin:/usr/bin:/bin"})
        return out.stdout + out.stderr


class IO:
    def __init__(self, dev, payload=None, version="emos-v0.10"):
        self.dev = dev
        self.clock = 0.0
        self.steps, self.records, self.cmds = [], [], []
        self.link = "same"
        self._payload = payload
        self.version = version
        self.drop = None          # substring of a command whose reply is lost
        self.relink_after_send = False

    async def sh(self, cmd, timeout):
        self.cmds.append(cmd)
        out = self.dev.run(cmd)
        if self.drop and self.drop in cmd:
            self.drop = None
            return ""
        return out

    async def send(self, data, dest):
        (self.dev.root / dest.lstrip("/")).write_bytes(data)
        if self.relink_after_send:
            self.link = "new"
        return ""

    async def payload(self, arch):
        if self._payload:
            return self._payload
        return new_init(arch), {}, self.version

    async def offload(self, fn, *args):
        return fn(*args)

    async def step(self, msg, log=True):
        self.steps.append(msg)

    async def record(self, version, build):
        self.records.append((version, build))

    async def sleep(self, seconds):
        self.clock += seconds
        self.dev.uptime += seconds
        took = self.dev.tick()
        if took:
            self.clock += took
            self.link = "new"

    def now(self):
        return self.clock

    def relink(self):
        # As em_api's does: a changed connection is reported once.
        link, self.link = self.link, ("gone" if self.link == "gone" else "same")
        return link


def run(io):
    return asyncio.run(up.run_update(io))


def refused(io):
    with pytest.raises(up.Refused) as e:
        run(io)
    return str(e.value)


@pytest.fixture
def old():
    return installed("emos-v0.9")


def untouched(dev, image):
    """The device is exactly as it started: same image, nothing left behind."""
    assert dev.image() == image
    assert not dev.mark.exists()
    assert not (dev.root / "data/emos/boot-new.img").exists()
    assert not (dev.root / "data/emos/boot-new.img.part").exists()
    assert not (dev.root / "reboot").exists()


# ── It works ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("arch", ["arm64", "arm"])
def test_an_update_ends_confirmed_on_the_new_image(tmp_path, arch):
    old = installed("emos-v0.9", arch=arch)
    dev = Device(tmp_path, old)
    io = IO(dev)
    assert run(io) == "emos-v0.10"
    dev.tick()

    new = dev.image()
    assert up.ramdisk_os_release(new)["VERSION_ID"] == "emos-v0.10"
    assert up.built_problems(old, new, "emos-v0.10", PART_BYTES) == []
    # Promoted, counter reset, nothing left behind.
    assert dev.good.read_bytes() == new
    assert dev.statef.read_text().strip() == "0"
    assert not dev.mark.exists()
    assert not (tmp_path / "data/emos/boot-new.img").exists()
    assert dev.boots == 1
    # Only the image's own length was written.
    assert len(dev.boot.read_bytes()) == PART_BYTES
    if len(new) == len(old):
        assert dev.boot.read_bytes()[len(new):len(new) + len(TAIL)] == TAIL
    assert io.records[0][0] == "emos-v0.9" and io.records[-1][0] == "emos-v0.10"


def test_the_fos6_payload_rides_into_the_image(tmp_path):
    old = installed("emos-v0.9", arch="arm")
    dev = Device(tmp_path, old)
    sbin = {"wpa_supplicant": b"W" * 300, "busybox": b"B" * 300, "em-wifi": b"#!/sbin/sh\n"}
    assert run(IO(dev, payload=(new_init("arm"), sbin, "emos-v0.10"))) == "emos-v0.10"
    import gzip
    ksz = int.from_bytes(dev.image()[8:12], "little")
    rsz = int.from_bytes(dev.image()[16:20], "little")
    roff = up.BLOCK + len(eb.pad(b"\0" * ksz))
    cpio = gzip.decompress(dev.image()[roff:roff + rsz])
    assert b"sbin/wpa_supplicant" in cpio and b"sbin/udhcpc" in cpio


def test_a_stale_known_good_is_refreshed_before_anything_is_written(tmp_path, old):
    dev = Device(tmp_path, old, good=installed("emos-v0.8"))
    io = IO(dev)
    dev.new_image_works = False
    msg = refused(io)
    # ...which is why the rollback lands on 0.9 and not on 0.8.
    assert "rolled back to emOS emos-v0.9" in msg
    assert dev.image() == old


def test_a_missing_known_good_is_created(tmp_path, old):
    dev = Device(tmp_path, old, good=None)
    assert run(IO(dev)) == "emos-v0.10"


# ── The new image is no good ─────────────────────────────────────────────────

def test_an_image_that_never_reaches_the_controller_is_rolled_back(tmp_path, old):
    dev = Device(tmp_path, old)
    dev.new_image_works = False
    io = IO(dev)
    msg = refused(io)
    assert "rolled back to emOS emos-v0.9" in msg
    assert dev.image() == old
    assert dev.boots == up.MAX_TRIES + 2      # three trials, the restore, old
    assert not dev.mark.exists()
    assert not (tmp_path / "data/emos/boot-new.img").exists()
    assert io.records[-1][0] == "emos-v0.9"
    # init said so itself, and the record does not outlive the report.
    assert "after 3 unconfirmed boots" in msg
    assert not (tmp_path / "data/emos/rollback.last").exists()


def test_a_write_that_does_not_read_back_is_undone_on_the_spot(tmp_path, old):
    dev = Device(tmp_path, old)
    dev.corrupt_writes = 2                    # the write, and its re-check
    io = IO(dev)
    msg = refused(io)
    assert "written back and verified" in msg and "safe to restart" in msg
    untouched(dev, old)
    assert dev.boots == 0


def test_a_restore_that_does_not_verify_says_not_to_restart(tmp_path, old):
    dev = Device(tmp_path, old)
    dev.corrupt_writes = 99
    io = IO(dev)
    msg = refused(io)
    assert msg.startswith("DO NOT RESTART OR UNPLUG")
    assert up.GOOD_IMG in msg and up.BOOT_DEV in msg and "TWRP" in msg
    assert not (tmp_path / "reboot").exists() and dev.boots == 0
    assert sum(f"if={up.GOOD_IMG} of=" in c for c in io.cmds) == 5


def test_a_lost_reply_to_the_write_is_settled_by_asking_the_flash(tmp_path, old):
    """The link drops mid-command: the dd finished, the answer never arrived."""
    dev = Device(tmp_path, old)
    io = IO(dev)
    io.drop = f"if={up.NEW_IMG} of="
    assert run(io) == "emos-v0.10"
    assert up.ramdisk_os_release(dev.image())["VERSION_ID"] == "emos-v0.10"


def test_a_device_that_does_not_restart_is_reported_as_that(tmp_path, old):
    dev = Device(tmp_path, old)
    dev.ignore_reboot = True
    io = IO(dev)
    msg = refused(io)
    assert "did not restart" in msg and "next time it restarts" in msg
    # The mark stays: the trial applies whenever it does restart.
    assert dev.mark.exists()


def test_a_shell_plane_that_is_slow_to_come_up_is_waited_for(tmp_path, old):
    """The new image is up and connected but its first status read gets no
    answer. That is not "did not restart", however long it takes."""
    dev = Device(tmp_path, old)
    io = IO(dev)
    real, silent = io.sh, {"left": 40}

    async def sh(cmd, timeout):
        if "UPTIME" in cmd and silent["left"] > 0:
            silent["left"] -= 1
            return ""
        return await real(cmd, timeout)
    io.sh = sh
    assert run(io) == "emos-v0.10"
    assert io.clock > 120


def test_a_redial_on_the_old_boot_is_not_reported_as_a_rollback(tmp_path, old):
    dev = Device(tmp_path, old)
    dev.ignore_reboot = True
    io = IO(dev)
    orig = io.sleep

    async def sleep(seconds):
        await orig(seconds)
        if io.clock > 30:
            io.link = "new"                   # same boot, new connection
    io.sleep = sleep
    msg = refused(io)
    assert "did not restart" in msg and "rolled back" not in msg
    assert dev.mark.exists()


# ── Refusals that change nothing ─────────────────────────────────────────────

def test_an_unconfirmed_boot_is_left_alone(tmp_path, old):
    dev = Device(tmp_path, old, state="1")
    assert "not confirmed" in refused(IO(dev))
    untouched(dev, old)


def test_a_partition_that_is_not_the_running_image_is_left_alone(tmp_path, old):
    dev = Device(tmp_path, old)
    (tmp_path / "os-release").write_text(
        'ID="emos"\nVERSION_ID="emos-v0.9"\nBUILD_ID="ffffffffffffffff"\n')
    assert "not the one running" in refused(IO(dev))
    untouched(dev, old)


def test_an_init_that_cannot_roll_back_is_not_installed(tmp_path, old):
    dev = Device(tmp_path, old)
    io = IO(dev, payload=(fx.fake_init(size=8192), {}, "emos-v0.10"))
    assert "cannot roll a failed update back" in refused(io)
    untouched(dev, old)


def test_an_init_for_the_other_kernel_is_not_installed(tmp_path, old):
    dev = Device(tmp_path, old)
    assert "not AArch64" in refused(IO(dev, payload=(new_init("arm"), {}, "emos-v0.10")))
    untouched(dev, old)


def test_a_read_that_never_verifies_is_refused(tmp_path, old):
    dev = Device(tmp_path, old)
    io = IO(dev)
    real = dev.run
    dev.run = lambda cmd: (real(cmd).replace("CHUNKMD5:", "CHUNKMD5:0")
                           if "CHUNK_BEGIN" in cmd else real(cmd))
    assert "could not read the boot partition" in refused(io)
    untouched(dev, old)


def test_a_redial_during_the_transfer_stops_before_the_write(tmp_path, old):
    dev = Device(tmp_path, old)
    io = IO(dev)
    io.relink_after_send = True
    assert "reconnected during the transfer" in refused(io)
    untouched(dev, old)


def test_nothing_is_written_to_the_partition_before_the_mark(tmp_path, old):
    dev = Device(tmp_path, old)
    io = IO(dev)
    run(io)
    writes = [i for i, c in enumerate(io.cmds) if f"of={up.BOOT_DEV}" in c]
    marks = [i for i, c in enumerate(io.cmds) if f"> {up.TRIAL_MARK}.tmp" in c]
    assert len(writes) == 1 and len(marks) == 1 and marks[0] < writes[0]


# ── With no update task watching ─────────────────────────────────────────────

def test_a_controller_that_restarted_still_confirms_on_connect(tmp_path, old):
    dev = Device(tmp_path, old)
    new = eb.build_emos_image(old, new_init(), "emos-v0.10")["image"]
    osr = up.ramdisk_os_release(new)
    part = bytearray(dev.boot.read_bytes()); part[:len(new)] = new
    dev.boot.write_bytes(bytes(part))
    dev.mark.write_text(up.trial_mark(up.stored_id(new), osr["BUILD_ID"], "emos-v0.10"))
    dev.reboot()
    assert dev.trial and dev.statef.read_text().strip() == "1"

    io = IO(dev)
    st, verdict = asyncio.run(up.settle_on_connect(io.sh))
    dev.tick()
    assert verdict == "confirm" and st["version"] == "emos-v0.10"
    assert not dev.mark.exists() and dev.good.read_bytes() == new
    assert dev.statef.read_text().strip() == "0"


def test_a_leftover_mark_on_the_old_image_reads_as_a_rollback(tmp_path, old):
    dev = Device(tmp_path, old)
    dev.mark.write_text(up.trial_mark("ab" * 20, "0123456789abcdef", "emos-v0.10"))
    st, verdict = asyncio.run(up.settle_on_connect(IO(dev).sh))
    assert verdict == "rolled_back" and st["version"] == "emos-v0.9"
    assert not dev.mark.exists()


def test_a_rollback_nobody_watched_is_noticed_and_cleaned_up(tmp_path, old):
    """C95, 2026-10-02: the controller was stopped through the trial. init
    restored the old image and removed the mark, so all that remained was the
    pushed image."""
    dev = Device(tmp_path, old)
    dev.new_image_works = False
    dev.writes_record = False                 # the rolled-back init is 0.9
    io = IO(dev)
    real_relink = io.relink
    io.relink = lambda: "gone" if dev.boots else real_relink()   # controller away
    refused(io)
    assert dev.image() == old and not dev.mark.exists()
    left = tmp_path / "data/emos/boot-new.img"
    left.write_bytes(b"x" * 4096)             # as the real run left it

    st, verdict = asyncio.run(up.settle_on_connect(IO(dev).sh))
    assert verdict == "incomplete" and st["version"] == "emos-v0.9"
    assert not left.exists()
    # ...and only once.
    assert asyncio.run(up.settle_on_connect(IO(dev).sh))[1] == "none"


def test_an_unwatched_rollback_is_reported_from_inits_own_record(tmp_path, old):
    dev = Device(tmp_path, old)
    dev.new_image_works = False
    io = IO(dev)
    real_relink = io.relink
    io.relink = lambda: "gone" if dev.boots else real_relink()   # controller away
    refused(io)
    rec = tmp_path / "data/emos/rollback.last"
    assert rec.exists() and dev.image() == old

    st, verdict = asyncio.run(up.settle_on_connect(IO(dev).sh))
    assert verdict == "rolled_back"
    assert st["rollback"]["tries"] == 3 and st["rollback"]["from"] != st["rollback"]["to"]
    assert "restored its previous image" in up.rollback_text(st["rollback"], st["version"])
    assert not rec.exists()
    assert not (tmp_path / "data/emos/boot-new.img").exists()
    assert asyncio.run(up.settle_on_connect(IO(dev).sh))[1] == "none"


def test_an_ordinary_connect_changes_nothing(tmp_path, old):
    dev = Device(tmp_path, old)
    st, verdict = asyncio.run(up.settle_on_connect(IO(dev).sh))
    assert verdict == "none" and st["version"] == "emos-v0.9"
    untouched(dev, old)
