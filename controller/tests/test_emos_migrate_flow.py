"""
FireOS 5 to emOS, run against a simulated Echo with a REAL shell.

Built on test_emos_update_flow's simulator: every command the migration sends
runs under `sh` with paths remapped, so the commands are under test. FireOS is
added around it: getprop/setprop/wpa_cli are scripts, the GPT names are
uevent files, /proc/version is the kernel's banner, and a FireOS image boots
with none of init's rollback (it has no emOS init). An emOS image boots by the
simulator's init rules, which trialcheck.c holds the real init to.
"""

import asyncio
import zlib

import pytest

import em_emos_build as eb
import em_emos_migrate as mg
import em_emos_update as up
import test_emos_build as fx
import test_emos_update_flow as fl
from test_emos_update import new_init

pytestmark = getattr(fl, "pytestmark", [])

BANNER = ("Linux version 3.18.19+ (build@fos) (gcc version 4.9) #1 SMP "
          "PREEMPT Fri Nov 18 04:47:40 UTC 2022")
PSK = "s3cret-passphrase"
CONF = ("ctrl_interface=/data/misc/wifi/sockets\n"
        "p2p_no_group_iface=1\n\n"
        "network={\n\tssid=\"Home Net\"\n\tscan_ssid=1\n"
        f"\tpsk=\"{PSK}\"\n\tkey_mgmt=WPA-PSK\n\tpriority=6\n}}\n")
STATUS = "ssid=Home Net\nkey_mgmt=WPA2-PSK\nwpa_state=COMPLETED\n"
SCAN = ("bssid / frequency / signal level / flags / ssid\n"
        "00:11:22:33:44:55\t5785\t-50\t[WPA2-PSK-CCMP][ESS]\tHome Net\n")
FIREOS_BOOT_S = 120.0


def fireos_image(banner=BANNER):
    """A FireOS 5 boot image: a gzip arm64 Image carrying the running
    kernel's banner, as Amazon's kernel carries linux_banner."""
    image = bytearray(0x100)
    image[0x38:0x3c] = b"ARM\x64"
    body = bytes(image) + banner.encode() + b"\n\0" + b"\0" * 2400
    co = zlib.compressobj(9, zlib.DEFLATED, 31)
    return fx.make_reference(zimage=co.compress(body) + co.flush())


class FireOS(fl.Device):
    """An Echo on FireOS 5, amonet 1, booted from slot A."""

    def __init__(self, root, image=None, **props):
        image = image or fireos_image()
        super().__init__(root, image, good=None, state="0")
        self.statef.unlink()                     # FireOS never wrote one
        r = self.root
        self.props = {"ro.build.version.release": "5.1.1",
                      "ro.boot.slot_suffix": "_a"}
        self.props.update(props.pop("props", {}))
        names = {"mmcblk0p10": "boot_a_x", "mmcblk0p13": "system_a",
                 "mmcblk0p3": "lk_a"}
        names.update(props.pop("names", {}))
        for part, name in names.items():
            (r / "sys" / part).mkdir(parents=True)
            (r / "sys" / part / "uevent").write_text(
                f"MAJOR=179\nDEVNAME={part}\nPARTNAME={name}\n")
        (r / "sys" / "mmcblk0p10" / "size").write_text(
            f"{fl.PART_BYTES // 512}\n")
        (r / "lk").write_bytes(bytes.fromhex(props.pop("lk", mg.AMONET1_LK))
                               + b"\0" * 4096)
        (r / "proc_version").write_text(props.pop("banner", BANNER) + "\n")
        (r / "data/misc/wifi").mkdir(parents=True)
        (r / "data/misc/wifi/wpa_supplicant.conf").write_text(
            props.pop("conf", CONF))
        (r / "wpa_status").write_text(props.pop("status", STATUS))
        (r / "wpa_scan").write_text(props.pop("scan", SCAN))
        self._script("getprop", "".join(
            f'[ "$1" = "{k}" ] && echo "{v}"\n' for k, v in self.props.items()))
        self._script("setprop", f'[ "$1 $2" = "sys.powerctl reboot" ] && '
                                f'touch {r}/reboot\n')
        self._script("wpa_cli", f'for a; do last=$a; done\n'
                                f'[ "$last" = status ] && cat {r}/wpa_status\n'
                                f'[ "$last" = scan_results ] && cat {r}/wpa_scan\n')

    def _script(self, name, body):
        p = self.root / "bin" / name
        p.write_text("#!/bin/sh\n" + body + "exit 0\n")
        p.chmod(0o755)

    def on_fireos(self):
        return not up.is_ours(self.image())

    def _load(self):
        osr = self.root / "os-release"
        if up.is_ours(self.image()):
            super()._load()
        else:
            osr.unlink(missing_ok=True)

    def reboot(self):
        """FireOS boots with no rollback logic at all; emOS boots by init's
        rules, and init restoring FireOS hands over to FireOS."""
        elapsed = 0.0
        while True:
            if self.on_fireos():
                self._load()
                self.trial = False
                self.boots += 1
                self.uptime = FIREOS_BOOT_S
                return elapsed + FIREOS_BOOT_S
            self.boots += 1
            self.uptime = 30.0
            elapsed += 30.0
            tries = int(self.statef.read_text() or 0) if self.statef.exists() else 0
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
                if self.mark.read_text().splitlines()[0:1] == [
                        up.stored_id(self.image())]:
                    self.trial = True
                else:
                    self.mark.unlink()
            if not self.trial:
                self._confirm()
                return elapsed
            if self.new_image_works:
                return elapsed
            elapsed += up.TRIAL_SECS

    def run(self, cmd):
        r = self.root
        for a, b in (
            ("/sys/class/block/", f"{r}/sys/"),
            ("/dev/block/mmcblk0p3", f"{r}/lk"),
            ("/proc/version", f"{r}/proc_version"),
            ("/data/misc/wifi/", f"{r}/data/misc/wifi/"),
            ("mkdir -p /data/emos", f"mkdir -p {r}/data/emos"),
            ("[ -d /data/emos ]", f"[ -d {r}/data/emos ]"),
        ):
            cmd = cmd.replace(a, b)
        return super().run(cmd)


class IO(fl.IO):
    def __init__(self, dev, **kw):
        super().__init__(dev, version="emos-v0.11", **kw)
        self.escrowed = None
        self.escrow_fails = ""
        self.send_fails = ""
        self.fail = None          # substring of commands whose replies are lost
        self.fail_n = 10 ** 6     # ...for this many of them

    async def sh(self, cmd, timeout):
        out = await super().sh(cmd, timeout)
        if self.fail and self.fail in cmd and self.fail_n > 0:
            self.fail_n -= 1
            return ""
        return out

    async def send(self, data, dest):
        return self.send_fails or await super().send(data, dest)

    async def escrow(self, data, md5):
        if self.escrow_fails:
            return self.escrow_fails
        self.escrowed = (data, md5)
        return ""


def run(io):
    return asyncio.run(mg.run_migration(io))


def refused(io):
    with pytest.raises(up.Refused) as e:
        run(io)
    return str(e.value)


def untouched(dev, image):
    """Still FireOS, and nothing of the migration left on /data."""
    assert dev.image() == image
    assert not dev.mark.exists()
    assert not dev.statef.exists()
    assert not (dev.root / "data/emos/boot-new.img").exists()
    assert not (dev.root / "reboot").exists()


# ── It works ─────────────────────────────────────────────────────────────────

def test_a_migration_ends_confirmed_on_emos(tmp_path):
    fireos = fireos_image()
    dev = FireOS(tmp_path, fireos)
    io = IO(dev)
    assert run(io) == "emos-v0.11"
    dev.tick()

    new = dev.image()
    assert up.ramdisk_os_release(new)["VERSION_ID"] == "emos-v0.11"
    assert mg.migrated_problems(fireos, new, "emos-v0.11", fl.PART_BYTES) == []
    # The controller kept FireOS; init promoted emOS once it was confirmed.
    assert io.escrowed[0] == fireos
    assert dev.good.read_bytes() == new
    assert dev.statef.read_text().strip() == "0"
    assert not dev.mark.exists()
    assert not (dev.root / "data/emos/boot-new.img").exists()
    # What lay past the image on the partition is untouched.
    assert fl.TAIL in dev.boot.read_bytes()[len(new):]
    assert io.records[-1][0] == "emos-v0.11"


def test_leftovers_of_an_earlier_emos_life_are_cleared(tmp_path):
    """A boot.state of 3 would roll the first boot straight back, and an old
    wpa.conf would be preferred over Android's."""
    dev = FireOS(tmp_path)
    dev.statef.write_text("3\n")
    old_conf = dev.root / "data/emos/wpa.conf"
    old_conf.write_text('network={\n\tssid="Old"\n}\n')
    (dev.root / "data/emos/rollback.last").write_text("from=x\n")
    assert run(IO(dev)) == "emos-v0.11"
    assert not old_conf.exists()
    assert (dev.root / "data/emos/wpa.conf.pre-migration").read_text() \
        .startswith("network=")
    assert dev.boots == 1


def test_the_psk_never_leaves_the_echo(tmp_path):
    dev = FireOS(tmp_path)
    out = dev.run(mg.preflight_cmd())
    assert up.OK in out and "Home Net" in out
    assert PSK not in out
    pre = mg.parse_preflight(out)
    assert mg.preflight_problems(pre) == []
    assert pre["lk"] == mg.AMONET1_LK and pre["banner"] == BANNER


# ── It falls back to FireOS ──────────────────────────────────────────────────

def test_an_emos_that_never_confirms_puts_fireos_back(tmp_path):
    fireos = fireos_image()
    dev = FireOS(tmp_path, fireos)
    dev.new_image_works = False
    msg = refused(IO(dev))
    assert "put FireOS back" in msg and "3 unconfirmed boots" in msg
    assert dev.image() == fireos
    assert dev.on_fireos()
    assert not dev.mark.exists()
    assert not (dev.root / "data/emos/rollback.last").exists()


def test_a_write_that_does_not_read_back_restores_fireos(tmp_path):
    fireos = fireos_image()
    dev = FireOS(tmp_path, fireos)
    dev.corrupt_writes = 2          # the write, and the re-verify reads it
    msg = refused(IO(dev))
    assert "written back and verified" in msg
    assert dev.image() == fireos
    assert not dev.mark.exists()
    assert not (dev.root / "reboot").exists()


def test_an_echo_that_ignores_the_restart_is_reported(tmp_path):
    dev = FireOS(tmp_path)
    dev.ignore_reboot = True
    assert "did not restart" in refused(IO(dev))
    assert up.is_ours(dev.image())      # written and verified, waiting


# ── It refuses, and changes nothing ──────────────────────────────────────────

@pytest.mark.parametrize("kw,why", [
    ({"props": {"ro.build.version.release": "7.1.2"}}, "only FireOS 5"),
    ({"props": {"ro.boot.slot_suffix": "_b"}}, "#973"),
    ({"names": {"mmcblk0p10": "boot_a"}}, "unfamiliar partition layout"),
    ({"names": {"mmcblk0p13": "system_b"}}, "not system_a"),
    ({"lk": "88168858407b0500"}, "not amonet 1's"),
    ({"conf": CONF.replace("Home Net", "Other")}, "no entry"),
    ({"status": "wpa_state=SCANNING\n"}, "not connected"),
    ({"scan": SCAN + "00:11:22:33:44:56\t2412\t-70\t[WPA-PSK-CCMP]\tHome Net\n"},
     "#1144"),
])
def test_preflight_refusals_change_nothing(tmp_path, kw, why):
    fireos = fireos_image()
    dev = FireOS(tmp_path, fireos, **kw)
    io = IO(dev)
    msg = refused(io)
    assert why in msg and msg.endswith(up.UNCHANGED)
    assert io.escrowed is None
    untouched(dev, fireos)


def test_an_image_that_is_not_the_running_kernel_is_refused(tmp_path):
    fireos = fireos_image(banner=BANNER.replace("Nov 18", "Dec 25"))
    dev = FireOS(tmp_path, fireos)
    io = IO(dev)
    assert "not the one running" in refused(io)
    assert io.escrowed is None
    untouched(dev, fireos)


def test_an_emos_image_on_p10_is_refused(tmp_path):
    ours = eb.build_emos_image(fireos_image(), new_init(), "emos-v0.10")["image"]
    dev = FireOS(tmp_path, ours)
    assert "already holds an emOS image" in refused(IO(dev))


def test_no_way_back_no_write(tmp_path):
    fireos = fireos_image()
    dev = FireOS(tmp_path, fireos)
    io = IO(dev)
    io.escrow_fails = "disk full"
    assert "could not keep a copy of the FireOS image: disk full" in refused(io)
    untouched(dev, fireos)


def test_an_init_without_the_trial_mark_is_refused(tmp_path):
    fireos = fireos_image()
    dev = FireOS(tmp_path, fireos)
    old = fx.fake_init(machine=183, elf_class=2, size=8192)
    io = IO(dev, payload=(old, {}, "emos-v0.9"))
    assert "cannot roll a failed boot back" in refused(io)
    assert dev.image() == fireos and not dev.mark.exists()


# ── The built image's own checks ─────────────────────────────────────────────

def test_migrated_problems_hold_everything_but_the_ramdisk_and_ramoops():
    ref = fireos_image()
    img = eb.build_emos_image(ref, new_init(), "emos-v0.11")["image"]
    assert mg.migrated_problems(ref, img, "emos-v0.11", 0) == []
    assert "identify as" in mg.migrated_problems(ref, img, "emos-v0.12", 0)[0]
    assert "partition holds" in mg.migrated_problems(ref, img, "emos-v0.11", 100)[0]
    other = fireos_image(banner=BANNER + "x")
    assert any("kernel differs" in p
               for p in mg.migrated_problems(other, img, "emos-v0.11", 0))
    bad = bytearray(img)
    bad[64:70] = b"XXXXXX"
    assert any("command line" in p for p in
               mg.migrated_problems(ref, bytes(bad), "emos-v0.11", 0))
    bad = bytearray(img)
    bad[20] ^= 1
    assert any("load addresses" in p for p in
               mg.migrated_problems(ref, bytes(bad), "emos-v0.11", 0))


def test_running_kernel_needs_the_whole_banner():
    ref = fireos_image()
    assert mg.running_kernel(ref, BANNER)
    assert not mg.running_kernel(ref, BANNER + " extra")
    assert not mg.running_kernel(ref, "")
    assert not mg.running_kernel(b"not an image", BANNER)


def test_no_verified_fireos_copy_for_init_no_write(tmp_path):
    """init's way back is boot-good.img; one that did not save is a refusal."""
    fireos = fireos_image()
    dev = FireOS(tmp_path, fireos)
    (dev.root / "data/emos/boot-good.img.new").mkdir()     # the dd cannot land
    assert "verified copy of the FireOS image" in refused(IO(dev))
    untouched(dev, fireos)


# ── Every other way it can stop ──────────────────────────────────────────────

def _good_pre(tmp_path):
    return mg.parse_preflight(FireOS(tmp_path).run(mg.preflight_cmd()))


@pytest.mark.parametrize("change,why", [
    ({"banner": ""}, "version could not be read"),
    ({"md5_ok": False}, "md5sum does not run"),
    ({"b64_ok": False}, "base64 does not run"),
    ({"header": b""}, "could not be read as a boot image"),
    ({"partition_bytes": 0}, "size could not be read"),
])
def test_each_preflight_fact_can_refuse(tmp_path, change, why):
    pre = _good_pre(tmp_path)
    assert mg.preflight_problems(pre) == []
    pre.update(change)
    assert any(why in p for p in mg.preflight_problems(pre))


def test_unanswered_and_unreadable_are_refusals():
    assert mg.parse_preflight("no marker") == {}
    assert mg.preflight_problems({}) == ["the Echo did not answer the pre-flight check"]
    assert "not one whole boot image" in mg.reference_problems(b"", BANNER)[0]
    assert not mg.running_kernel(fx.make_reference(zimage=fx.arm_zimage()), BANNER)
    assert mg.parse_scan("short\tline\n") == []
    ref = fireos_image()
    img = eb.build_emos_image(ref, new_init(), "emos-v0.11")["image"]
    assert "header does not describe" in mg.migrated_problems(ref, img[:-2048], "emos-v0.11", 0)[0]
    bad = bytearray(img)
    ksz = int.from_bytes(img[8:12], "little")
    bad[eb.PAGE + -(-ksz // eb.PAGE) * eb.PAGE + 10] ^= 1     # inside the ramdisk
    assert any("id does not match" in p for p in
               mg.migrated_problems(ref, bytes(bad), "emos-v0.11", 0))


def _refused_unchanged(tmp_path, setup, why, fireos_kept=True):
    fireos = fireos_image()
    dev = FireOS(tmp_path, fireos)
    io = IO(dev)
    setup(dev, io)
    msg = refused(io)
    assert why in msg, msg
    if fireos_kept:
        assert dev.image() == fireos and not dev.mark.exists()
        assert not (dev.root / "reboot").exists()
    return dev, io


def test_no_space(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "space_problem", lambda *a: "not enough space")
    _refused_unchanged(tmp_path, lambda d, i: None, "not enough space")


@pytest.mark.parametrize("fail,why", [
    ("CHUNK_BEGIN", "could not read the boot partition"),
    ("PREP:ok", "earlier emOS left on /data"),
    ("boot-good.img.new", "changed while it was being read"),
    ("MARK_BEGIN", "could not write the trial mark"),
])
def test_a_lost_reply_before_the_write_refuses(tmp_path, fail, why):
    def setup(dev, io):
        io.fail = fail
    _refused_unchanged(tmp_path, setup, why)


def test_payload_problems_refuse(tmp_path, monkeypatch):
    def bad_version(dev, io):
        io._payload = (new_init(), {}, "bad version!")
    _refused_unchanged(tmp_path, bad_version, "not a usable version string")

    def wrong_arch(dev, io):
        io._payload = (new_init("arm"), {}, "emos-v0.11")
    _refused_unchanged(tmp_path / "b", wrong_arch, "")

    monkeypatch.setattr(mg, "migrated_problems", lambda *a: ["packer misbehaved"])
    _refused_unchanged(tmp_path / "c", lambda d, i: None, "packer misbehaved")


def test_transfer_problems_refuse(tmp_path):
    def send_fails(dev, io):
        io.send_fails = "md5 mismatch"
    dev, _ = _refused_unchanged(tmp_path, send_fails, "md5 mismatch")
    assert not (dev.root / "data/emos/boot-new.img").exists()

    def relinks(dev, io):
        io.relink_after_send = True
    _refused_unchanged(tmp_path / "b", relinks, "reconnected during the transfer")


def test_a_lost_write_reply_is_settled_by_reading_the_flash(tmp_path):
    dev = FireOS(tmp_path)
    io = IO(dev)
    io.drop = f"of={up.BOOT_DEV}"
    assert run(io) == "emos-v0.11"


def test_fireos_back_with_no_rollback_record_is_said_plainly(tmp_path):
    dev = FireOS(tmp_path)
    dev.new_image_works = False
    dev.writes_record = False
    assert "without starting emOS" in refused(IO(dev))
    assert dev.on_fireos()


def test_an_unanswered_status_is_asked_again(tmp_path):
    dev = FireOS(tmp_path)
    io = IO(dev)
    io.fail, io.fail_n = "RB_BEGIN", 2
    assert run(io) == "emos-v0.11"


class _Gone(IO):
    """The Echo restarts and never comes back."""

    async def sleep(self, seconds):
        await super().sleep(seconds)
        if self.link == "new":
            self.link = "gone"


def test_an_echo_that_never_returns_says_where_to_look(tmp_path):
    dev = FireOS(tmp_path)
    msg = refused(_Gone(dev))
    assert "TWRP and a USB cable" in msg and "kept on the controller" in msg


class _Redial(IO):
    """The link redials once before FireOS has restarted at all."""

    async def sleep(self, seconds):
        # Before the tick, which takes the restart request away.
        if (self.dev.root / "reboot").exists() and not getattr(self, "_done", 0):
            self._done = 1
            self.redialled = True
            await super().sleep(seconds)
            self.link = "new"
            return
        await super().sleep(seconds)


def test_a_redial_before_the_restart_is_not_a_rollback(tmp_path):
    dev = FireOS(tmp_path)
    dev.ignore_reboot = True
    dev.uptime = 99999.0
    io = _Redial(dev)
    assert "did not restart" in refused(io)
    assert io.redialled
    # It asked the redialled link, saw the old kernel still up, and waited.
    assert any("RB_BEGIN" in c for c in io.cmds)


def test_a_confirmation_that_does_not_land_is_sent_again(tmp_path):
    dev = FireOS(tmp_path)
    io = IO(dev)
    io.fail, io.fail_n = f"rm -f {up.TRIAL_MARK} {up.ROLLBACK_REC}", 1
    assert run(io) == "emos-v0.11"
    assert not dev.mark.exists()


def test_reference_must_be_fireos_and_64_bit():
    ours = eb.build_emos_image(fireos_image(), new_init(), "emos-v0.10")["image"]
    assert "already holds an emOS image" in mg.reference_problems(ours, BANNER)[0]
    arm = fx.make_reference(zimage=fx.arm_zimage())
    assert any("not the 64-bit" in p for p in mg.reference_problems(arm, BANNER))
    # A conf line with no `=` is skipped. hostap refuses such a conf whole,
    # so one the running supplicant loaded cannot hold it; skipping is enough.
    assert mg.parse_conf_networks('network={\n\tjunk\n\tssid="A"\n}\n')[0]["ssid"] == b"A"


def test_back_on_some_other_emos_is_reported(tmp_path, monkeypatch):
    other = eb.build_emos_image(fireos_image(), new_init(), "emos-v0.10")["image"]
    dev = FireOS(tmp_path)
    orig = dev.reboot

    def reboot():
        part = bytearray(dev.boot.read_bytes())
        part[:len(other)] = other
        dev.boot.write_bytes(bytes(part))
        dev.mark.unlink(missing_ok=True)
        return orig()
    dev.reboot = reboot
    assert "not the image written" in refused(IO(dev))


# ── 2026-11-11 review: what a refusal leaves, and the commands FireOS runs ──

def test_a_refusal_after_prepare_puts_back_what_it_moved(tmp_path):
    """The send fails after PREPARE and refresh_good ran: the old wpa.conf
    comes back, no image, copy or mark is left, and the message does not
    claim nothing changed."""
    dev = FireOS(tmp_path)
    old_conf = dev.root / "data/emos/wpa.conf"
    old_conf.write_text('network={\n\tssid="Old"\n}\n')
    io = IO(dev)
    io.send_fails = "link dropped"
    msg = refused(io)
    assert "sending the image failed" in msg
    assert "boot partition are unchanged" in msg and "nothing was changed" not in msg
    assert old_conf.read_text().startswith("network=")
    assert not (dev.root / "data/emos/wpa.conf.pre-migration").exists()
    for f in ("boot-good.img", "boot-new.img", "update.pending"):
        assert not (dev.root / "data/emos" / f).exists(), f
    assert dev.on_fireos() and not (dev.root / "reboot").exists()


def test_a_failed_write_probe_refuses_and_tidies(tmp_path):
    dev = FireOS(tmp_path)
    io = IO(dev)
    io.fail = "conv=notrunc,fsync 2>/dev/null && echo DDTOOL"
    msg = refused(io)
    assert "busybox dd cannot write" in msg and "nothing was changed" in msg
    assert not io.escrowed


def test_the_preflight_writes_nothing(tmp_path):
    dev = FireOS(tmp_path)
    cmd = mg.preflight_cmd()
    assert "mkdir" not in cmd and "of=" not in cmd and "dd if=/dev/zero" not in cmd


def test_the_mark_is_gone_before_a_write_back_that_fails(tmp_path):
    """Write and write-back both fail: no mark may name the unverified image,
    or settle_fireos would report emOS as waiting for a restart."""
    dev = FireOS(tmp_path)
    dev.corrupt_writes = 99
    msg = refused(IO(dev))
    assert "DO NOT RESTART" in msg
    assert not dev.mark.exists()


def test_fireos_commands_name_busybox_for_what_fireos_5_lacks():
    """FireOS 5's shell has no printf, sed, head, grep or which on its path
    (docs/history/controller.md, 2026-11-10)."""
    import re
    cmds = [mg.preflight_cmd(), mg.PROBE_CMD, mg.PREPARE_CMD, mg.UNDO_CMD,
            mg.settle_cmd(), up.mark_cmd("a" * 40, "b", "v", printf="busybox printf")]
    for c in cmds:
        for tool in ("printf", "sed", "head", "grep", "which", "md5sum",
                     "base64", "dd", "tail"):
            for m in re.finditer(rf"(?<![\w-]){tool}\b", c):
                assert c[:m.start()].endswith("busybox "), (tool, c[max(0, m.start() - 40):m.end()])


def test_the_restart_is_asked_before_ok():
    """The shell is closed when OK arrives; setprop must already have run."""
    assert mg.REBOOT_CMD.index("setprop") < mg.REBOOT_CMD.index(up.OK)
