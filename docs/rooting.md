# Rooting the Echo Dot Gen 2 (biscuit)

> **You do this at your own risk. We accept no responsibility for negative
> outcomes experienced.**

> **Which amonet version?** Either. **v1.1.0** leaves the Echo on FireOS 5,
> where the wizard offers emOS or FireOS with root. **v2.0.0** moves it to
> FireOS 6, where the wizard offers emOS. Moving between amonet versions is
> part of the unlock, which is R0rt1z2's work: follow the XDA thread for it.

EchoMuse needs an Echo Dot Gen 2 that is already unlocked and running
FireOS 5, or FireOS 6 for emOS. Two separate jobs get you there, and they carry very different
risk.

## Hardware

- Amazon Echo Dot 2nd Gen (RS03QR, 2016)
- Codename: biscuit
- SoC: MediaTek MT8163, quad-core ARM Cortex-A53 @ 1.5GHz
- RAM: 512MB
- OS: FireOS 5 (Android 5.1, API 22) — see the note below on FireOS 6
- MicroUSB cable required

> **Which FireOS boots depends on which amonet you used.** Up to v1.1.0, only
> FireOS 5 based ROMs boot after unlocking; R0rt1z2's thread said flashing
> FireOS 6 "may result in a (soft) brick". v2.0.0 (10 September 2026) turns
> that round: it boots FireOS 6, and FireOS 5 no longer boots.
>
> What changes is the bootloaders, not the kernel's bitness. The v2.0.0
> installer writes a newer preloader, LK and TrustZone to the device, and
> FireOS 5's kernel does not run on them. Its LK patch starts a 32-bit or
> 64-bit kernel according to what the boot image asks for, so bitness was not
> the obstacle. (This corrects an earlier version of this note, which put it
> down to the TrustZone signature chain.)
>
> **The firmware runs on FireOS 5; emOS runs on both.** EchoMuse's own
> firmware targets the Android userspace, which is 32-bit on either FireOS, so
> the binary is the same. What differs is emOS's init, which has to match the
> kernel — 64-bit on FireOS 5, 32-bit on FireOS 6 — and the wizard picks that
> by reading the architecture out of your own escrowed boot image rather than
> asking you.
>
> The FireOS flow needs **v1.1.0**, since it boots the device's own Android 5.
> For that
> version, `Fire OS 6.5.7.0 (NS6570/6077)` still matters: its instructions
> tell you to update *to* it before unlocking, because the exploit downgrades
> the firmware partitions on the way through.

## What you need

For the unlock itself (R0rt1z2's thread has the authoritative list):

- **Linux machine** with ADB and fastboot installed — see the note below on macOS
- Python 3 (for boot image patching and Magisk DB creation)
- The following files downloaded and ready:
  - `amonet-biscuit-v1.1.0.zip` or `v2.0.0` — from R0rt1z2's XDA thread.
    v2.0.0 means emOS; see the note at the top of this page.
  - `update-kindle-csm_biscuit-272.6.8.0_user_680767620.bin` — FireOS 5 firmware
    (**this exact build** — see below)
  - `f1r30s.zip` — from R0rt1z2's XDA thread. Does four things, not one:
    enables ADB and UART console access, blocks Amazon's OTA domains in
    `/system/etc/hosts` so the device cannot update itself, and disables
    dm-verity. **Always flash it after a stock firmware image or the OS
    will not boot** — a stock flash restores verity against a partition
    table the unlock modified.
  - `Magisk-v17.3.zip` — from [GitHub](https://github.com/topjohnwu/Magisk/releases/tag/v17.3)
  - `server` — compiled EchoMuse binary (ARM, API 22)

> **Which FireOS 5 build?** R0rt1z2's thread lists six that boot on an
> unlocked Dot, and EchoMuse is developed and tested against exactly one:
> **Fire OS 5.5.5.4**, `272.6.8.0_user_680767620`. Every device in the
> project's own fleet runs it. The older builds are not known to be broken —
> they are simply untested here, and firmware defaults differ between builds
> in ways that reach USB and ADB behaviour. If you are choosing, choose this
> one. If you already have a device on another build and something behaves
> oddly, that is the first thing to mention when reporting it.
>
> Check what you have with `adb shell getprop ro.build.version.name`. The
> provisioning wizard reads it at the first step and says so in the log.

> **Why Magisk 17.3?** Newer versions dropped support for Android 5.1 (API 22). 25.x installs but the daemon silently fails. 17.3 is the last version that works reliably on this device.

> **Use Linux for the unlock.** `brick.sh` has been reported failing on
> macOS, and the project's own devices were unlocked from a Linux install on
> a Mac rather than from macOS itself. A live USB is enough — the unlock is
> the only step that needs it. The unlock is R0rt1z2's work, so questions
> about it belong on the XDA thread; this is only a note about what has been
> observed to work.
>
> **Linux ADB stability:** Linux aggressively power-manages USB devices by default, causing ADB disconnects. Disable autosuspend before starting: `echo -1 | sudo tee /sys/bus/usb/devices/*/power/autosuspend`.

For the EchoMuse half, the provisioning wizard needs only a **Chromium-based
browser** (Chrome or Edge — it talks to the device over WebUSB) and a running
controller. It fetches the firmware itself, so the `server` binary above is
only needed if you are provisioning by hand.

---

## Unlocking the device — R0rt1z2's amonet-biscuit

The persistent unlock, the bootrom exploit and TWRP for this device are
**R0rt1z2's** work, documented and maintained here:

- [amonet-biscuit — unlock, root, TWRP, unbrick](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/)
  on XDA Forums

Follow that thread, not this page. We link to it rather than copying it
because a copy goes out of date without anyone noticing. If the two ever
disagree, the thread is correct.

**This is the part that can ruin a device.** It runs a bootrom exploit,
modifies the partition table and wipes userdata. A failure here can leave a
Dot soft-bricked badly enough that recovery means opening the case and
shorting contacts on the board. Read the thread first, and do not start on a
device you cannot afford to lose.

## Where EchoMuse picks up

Everything below assumes you already have:

- An Echo Dot Gen 2 (**biscuit**) with the persistent unlock applied
- **TWRP** installed and bootable
- **FireOS 5** (Android 5.1) sideloaded

Once those are done, EchoMuse takes over. The provisioning wizard in the
dashboard handles the rest — see the [Quickstart](quickstart.md). It starts
from a device already in that state; it does not run the exploit.

### The wizard has two flows, and they write different things

**This matters before you start, because they are not equally reversible.**

- **emOS** — the current default. Nine steps, all inside TWRP. It escrows your
  boot partition and hands you the file, then replaces that partition with an
  image built from your own kernel and device trees plus our init. The result
  runs no Amazon userspace at all. See [`emos/README.md`](../emos/README.md).
- **FireOS** — thirteen steps, chosen on the wizard's first step (or by
  adding `?flow=fireos` to the dashboard URL).
  Keeps Android and adds root: the SELinux cmdline patch, Magisk, and the
  root-grant database. This is the path every device in the field took.

Both leave a failed step with the device still in TWRP and say so. The
difference that matters afterwards:

**A device on emOS cannot be re-provisioned by the wizard, and going back
wipes it.** emOS runs no adbd — it cannot, since adbd needs Android's property
service — so the wizard's first step finds no device to talk to. Returning to
FireOS means booting TWRP by hand, wiping cache and data, sideloading the
FireOS 5 image and then flashing `f1r30s.zip`. That erases `/data`, taking
EchoMuse, its configuration and its credentials with it. **`f1r30s.zip` is not
optional** — a stock flash restores dm-verity against a partition table the
unlock modified, and without it the device does not boot.

Keep the escrowed boot image the emOS flow hands you at step 3. Writing it back
takes about ten seconds, leaves `/data` alone, and is the undo for everything
below.

## What EchoMuse writes, and what it does not

This device has several layers below the operating system, and EchoMuse only
ever writes the FireOS one. Lowest first:

| Layer | What it is | Written by EchoMuse |
|---|---|---|
| Preloader | First stage of boot. Tracks boot attempts per slot. | No |
| LK (bootloader) | What `lk_build_desc` and `unlock_status` come from. amonet patches this. | No |
| amonet's unlock payload | Chainloads the real kernel. `mmcblk0p17` / `p18`. | No |
| TWRP (recovery) | | No, the wizard only runs commands inside it |
| FireOS kernel and ramdisk | `mmcblk0p10` / `p11`. | **Yes**, one write |
| `/system`, `/data` | FireOS userspace. | Yes, files only |

There is a single partition write either way, and TWRP presents that partition
as `/dev/block/other-boot`. What goes into it differs by flow: the **FireOS**
flow's Patch Boot Image step adds the SELinux permissive cmdline and the
`service echomuse` init entry to the kernel already there, while the **emOS**
flow replaces the partition with an image rebuilt from that same kernel and
device trees. Neither touches any layer above `No` in the table.

**The by-name directory means different things in TWRP and in Android**, which
is worth knowing before reading any of it as gospel. Measured on hardware:

| by-name entry | In TWRP | In Android |
|---|---|---|
| `boot_a` | `p10`, the kernel | `p17`, the payload |
| `boot_a_x` | `p10`, the kernel | `p10`, the kernel |
| `boot_a_amonet` | `p17`, the payload | not present |

TWRP remaps the bare names onto the kernel partitions and exposes the payload
explicitly as `*_amonet`. So the same name means opposite things depending on
where you are standing, and `p10` answers to two names at once.

Before writing, the wizard resolves `/dev/block/other-boot`, collects every
by-name alias of whatever it points at, and refuses if any of them is an
amonet payload partition. Writing a kernel there would destroy the unlock and
mean running amonet again, so it is checked rather than assumed. It also
verifies the image it read is a real boot image before patching it, and reads
the cmdline back off the partition afterwards rather than trusting that the
write succeeded.

If any of those checks fail the wizard stops with the device still in TWRP,
which is a recoverable place to be.

## If a device will not boot

Anything at or below the bootloader is the unlock's territory, and
[R0rt1z2's thread](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/)
is the authority on it. One thing from that thread is worth repeating here
because it is time-critical:

> **Stop trying to boot it.** The preloader tracks boot attempts per slot, and
> if both slots run out of attempts the device stops booting altogether.

That state is recoverable, but the easy routes are gone: getting back in means
opening the case and shorting a pin on the board to reach the bootrom, which
R0rt1z2's thread documents and describes as not especially difficult. A device
sitting in fastboot or TWRP on the end of a cable needs none of that.
Repeatedly power cycling one that will not boot is what turns the first
situation into the second.

## How well tested is this?

**The two flows have very different amounts of evidence behind them, and the
default is the newer one.**

Eight devices have been through the **FireOS** flow's steps without a failure.
That is a small sample, all of it on the same model by the same person, so
treat it as encouraging rather than conclusive.

The **emOS** flow has completed end to end on hardware, but only recently and
on a handful of devices. Its first full run against a device restored to
genuine stock failed at four separate steps before it worked — all four were
faults in the wizard's own checks rather than in the writes, and all four are
fixed, but that is the maturity to price in. If you want the better-evidenced
path today, pick FireOS on the wizard's first step.

## Recovery

**The rule that matters: if a device will not boot, do not keep power cycling
it.** Repeatedly power cycling one that will not come up is what turns a
device recoverable from a cable into one that needs the case opened and a pin
shorted. Go to TWRP instead — it is one button combo away and it costs about
ten seconds to put the old boot image back.

**Reaching TWRP on a device that will not boot:**

1. Unplug the power.
2. Hold **mute** or **+** (volume up) down, and keep holding it. Which button
   depends on which amonet version unlocked the device; [R0rt1z2's
   thread](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/) has it.
3. Apply power with the button still held.
4. Wait for the ring to change — that is the confirmation you are in recovery,
   and you can let go once you see it.

`adb reboot recovery` is the easy route and it needs a device that is already
up, which is exactly what you do not have here.

### If a wizard step failed

The device is still in TWRP and the wizard says so. Reconnect and use
**Restore escrowed boot image** — it writes back the image read off your own
device at the escrow step, verifies it against the partition, and leaves
`/data` untouched. If you have reloaded the page since, choose the
`echomuse-stock-boot-*.img` file you downloaded at that step; it is the same
bytes.

### If the first boot after flashing emOS does not come up

The light ring says which case you are in:

| Ring | What it means | What to do |
|---|---|---|
| Filling, then white, then fading | Up and on the network | Nothing — done, about 30 seconds |
| Two lit segments at the bottom, throbbing | Waiting for the network | Nothing — this is most of the boot |
| Solid amber | emOS is restoring its own last known-good image | **Leave it.** It reboots itself |
| Red, stopped | A boot stage failed | Recoverable — go to TWRP and restore |
| One segment orbiting a full blue ring, for more than a minute | emOS never started | Go to TWRP and restore |

Restore the image from TWRP if the ring ends red and stopped, or as a single
segment orbiting a full blue ring for more than a minute. Anything else means
emOS is starting, so leave it to finish (#642). The orbit means the kernel
came up and our init never ran, so nothing on the device is going to fix
itself — it is the kernel's own boot animation, still running because
userspace never claimed the ring.

The amber row is worth knowing about precisely so you *do not* intervene:
emOS counts boots that never reached the network and, after three, puts its
own known-good image back and reboots. Interrupting that is the one way to
make it worse.

### If it will not reach TWRP either

That is the unlock's territory rather than ours, and R0rt1z2's thread covers
recovery and unbricking.

## Credits

- **R0rt1z2** — [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/):
  persistent unlock, TWRP and unbrick for this device
- **Dragon863** — [EchoCLI](https://github.com/Dragon863/EchoCLI): tethered root research
- **Binozo** — [GoTinyAlsa](https://github.com/Binozo/GoTinyAlsa) and the original EchoGo SDK

---

# Manual reference

The wizard performs the steps below for you. They are kept here for anyone
provisioning by hand, debugging a wizard step, or wanting to know exactly what
is being done to their device before letting something do it automatically.

## Step 3 — Patch the Boot Image for SELinux Permissive

This is the step that isn't documented anywhere else.

The Little Kernel (LK) bootloader hardcodes `androidboot.selinux=enforce` into the kernel command line — this is set before Android even loads, and it's what blocks every attempt to disable SELinux at runtime. You cannot `setenforce 0` as shell, you cannot `resetprop`, you cannot use `magiskpolicy`. The kernel won't let you.

The fix: we append `androidboot.selinux=permissive` to the boot image's own cmdline field. LK splices that field into the middle of its own parameters and adds its `enforce` afterwards, so both values end up on the kernel command line with ours first — and **the first one wins**. `androidboot.*` becomes a `ro.boot.*` property through Android's init, read-only properties are write-once, and the second set is refused. Measured on two devices, 2026-09-06: `getenforce` Permissive, `ro.boot.selinux` permissive.

Do not reason about this as a kernel parameter, where a later value would override an earlier one. `androidboot.selinux` is not one — the kernel's own switches are `selinux=` and `enforcing=`, which nothing here sets.

> **Note:** The cmdline is a null-terminated ASCII string in a 512-byte field at a fixed offset (byte 64) of the Android boot image header. We patch it directly rather than using magiskboot, which doesn't support cmdline modification on this version.

### From TWRP, extract magiskboot and pull the boot image:

```bash
adb shell 'mkdir -p /tmp/work /tmp/bin'
adb shell 'unzip /sdcard/f1r30s.zip bin/magiskboot -d /tmp/'
adb shell 'chmod 755 /tmp/bin/magiskboot'
adb shell 'dd if=/dev/block/other-boot of=/tmp/work/boot.img bs=1048576'
adb pull /tmp/work/boot.img boot_fresh.img
```

### Patch the cmdline on your host machine:

This **appends** to what FireOS already put there. An earlier version of these
instructions zeroed the whole 512-byte field and wrote a short replacement,
which silently discarded FireOS's own arguments — `rootwait`, `ro`,
`init=/init`, `buildvariant`, the `lowmemorykiller` tuning and `veritykeyid`.
Devices booted anyway, because LK supplies `root=` and `androidboot.hardware`
and kernel defaults covered the rest, so it went unnoticed for a long time. It
is still the wrong thing to do to somebody's boot image.

```python
python3 - <<'EOF'
ARG = b'androidboot.selinux=permissive'
START, END = 64, 576          # the 512-byte cmdline field

with open('boot_fresh.img', 'rb') as f:
    data = bytearray(f.read())

if bytes(data[:8]) != b'ANDROID!':
    raise SystemExit("Not an Android boot image — refusing to patch.")

field = data[START:END]
used  = field.index(0) if 0 in field else len(field)
existing = bytes(field[:used])
print("Old cmdline:", existing.decode(errors='replace'))

if ARG in existing.split():
    print("Already patched — nothing to do.")
elif any(a.startswith(b'androidboot.selinux=') for a in existing.split()):
    raise SystemExit(
        "This image already sets androidboot.selinux to something else. "
        "Appending would lose to it, because the FIRST value wins. Fix that "
        "value rather than adding a second one.")
else:
    addition = (b' ' if used else b'') + ARG
    if used + len(addition) >= len(field):      # keep room for the terminator
        raise SystemExit("Cmdline too long to append without truncating it.")
    data[START + used:START + used + len(addition)] = addition
    data[START + used + len(addition)] = 0
    print("New cmdline:", bytes(data[START:START + used + len(addition)]).decode())

    with open('boot_patched.img', 'wb') as f:
        f.write(data)
    print("Written to boot_patched.img")
EOF
```

Check that the new cmdline is your original one with `androidboot.selinux=permissive` on the end, and nothing missing from the front.

### Flash the patched image:

```bash
adb push boot_patched.img /tmp/work/boot_patched.img
adb shell 'dd if=/tmp/work/boot_patched.img of=/dev/block/other-boot bs=1048576'
adb reboot
```

### Verify:

```bash
adb shell getenforce
# Expected: Permissive
```

Check the kernel cmdline in logcat to confirm both values are present:

```
androidboot.selinux=permissive androidboot.selinux=enforce
```

Both appear — LK always appends its value after ours — but the device ends up in permissive mode.

---

## Step 4 — Install Magisk 17.3

With SELinux permissive, Magisk's daemon can now start and run properly.

```bash
adb reboot recovery
adb push Magisk-v17.3.zip /sdcard/
adb shell twrp install /sdcard/Magisk-v17.3.zip
adb reboot
```

Do **not** try `adb shell su -c id` yet — it will hang. The grant prompt requires a screen to approve, and the Echo Dot has no screen.

---

## Step 5 — Pre-seed the Magisk Grant Database

Magisk's `su` hangs on a screenless device because it's waiting for the user to tap "Grant" on a dialog that never appears. The fix is to create the policy database ourselves and push it before booting.

### On your host machine:

```python
python3 - <<'EOF'
import sqlite3
conn = sqlite3.connect('magisk.db')
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS policies
             (uid INTEGER, package_name TEXT, policy INTEGER,
              until INTEGER, logging INTEGER, notification INTEGER)''')
# uid 2000 = shell, policy 2 = always grant
c.execute("INSERT INTO policies VALUES (2000, 'com.android.shell', 2, 0, 1, 0)")
c.execute("INSERT INTO policies VALUES (0, 'root', 2, 0, 1, 0)")
conn.commit()
conn.close()
print("Done — magisk.db created")
EOF
```

### Push from TWRP:

```bash
adb reboot recovery
adb push magisk.db /data/adb/magisk.db
adb shell chmod 600 /data/adb/magisk.db
adb reboot
```

### Verify root:

```bash
adb shell su -c id
# Expected: uid=0(root) gid=0(root) context=u:r:magisk:s0
```

If you see `uid=0(root)` — you have persistent root. Reboot again and confirm it survives.

---

## Step 6 — Disable the Alexa Stack

With root, `pm disable` now works. Run these one at a time:

```bash
# Core Alexa voice pipeline
adb shell su -c 'pm disable amazon.speech.davs.davcservice'
adb shell su -c 'pm disable amazon.speech.sim'
adb shell su -c 'pm disable com.amazon.alexa.beaconbroadcaster'
adb shell su -c 'pm disable com.amazon.alexa.externalmediaplayer.fireos'
adb shell su -c 'pm disable com.amazon.wha.mediabrowserservice'

# Whisperjoin (Alexa device provisioning/cloud)
adb shell su -c 'pm disable com.amazon.whisperjoin.middleware'
adb shell su -c 'pm disable com.amazon.whisperjoin.wss.wifiprovisioner'

# Smart home and media agent (crash-loop after disabling above)
adb shell su -c 'pm disable com.amazon.device.smarthome.dshs.services'
adb shell su -c 'pm disable com.amazon.mediaplayeragent'

# WiFi management — only needed if you intend to reconfigure WiFi away from
# whatever network Alexa setup originally connected to. Both actively fight
# manual wpa_supplicant.conf edits by re-asserting their own saved network
# profile. See v2.5.0 changelog for the full investigation.
adb shell su -c 'pm disable com.amazon.android.service.wifiprofilemanager'
adb shell su -c 'pm disable com.amazon.device.smarthome.adapters.wifi'
# pm disable above does NOT stop the native SmartHomeWifid binary — it's
# launched by init via a property trigger chain, not as a normal package
# component. This durably prevents that trigger from ever firing:
adb shell su -c 'setprop persist.wifi.migrate.complete 0'
```

Reboot and check logcat. You should see "Unable to start service" messages for these packages — that's expected and harmless. No crash loops.

> **Keep `com.amazon.device.echoaudioservice` enabled.** This service initialises the MediaTek audio DSP at boot. Without it, the I2S clock never starts and audio playback will hang silently. You can disable Alexa's voice stack without touching this service.
>
> **What echoaudioservice actually does:** The APK is a stub (manifest only, no Java classes). It triggers `audio.primary.mt8163.so` (the MT8163 audio HAL) to initialise the DSP when Android starts the service. The HAL does all the real work — echoaudioservice is just the trigger.

---

## Step 7 — Disable WiFi Direct (p2p0)

The device has a WiFi Direct interface (`p2p0`) that interferes with mDNS multicast interface selection. It must be brought down before EchoMuse starts.

This is handled in `start_server.sh` — no manual action needed if you're following the full guide. If testing manually, run:

```bash
adb shell su -c 'ip link set p2p0 down'
```

---

---

## Step 8 — Install EchoMuse

EchoMuse runs as a Go binary on the device. It abstracts the hardware (mic, speaker, LEDs, buttons) and connects outbound to the EchoMuse controller over two persistent WebSocket connections (plus a demand-opened shell plane). There is no HTTP server on the device — no inbound ports, no iptables rules required.

### Set up the binary directory (A/B slots):

EchoMuse v2.4.4+ uses A/B slots: `server_a` and `server_b` with `/data/local/bin/server` as a symlink. This allows instant rollback without a binary transfer.

```bash
adb shell "su -c 'mkdir -p /data/local/bin'"
adb push server /sdcard/server
adb shell "su -c 'cp /sdcard/server /data/local/bin/server_a && chmod 755 /data/local/bin/server_a && ln -sf server_a /data/local/bin/server && chown root:root /data/local/bin/server_a'"
```

`server_b` starts empty. The first OTA update from the dashboard populates it.

### Create the startup script:

The canonical script is **`controller/device_payloads/start_server.sh`** in the repo (`device/scripts/start_server.sh` is a symlink to it) — the controller serves that exact file at `/api/provision/start_script` (this is what the provisioning wizard installs), read from disk per request. Don't hand-maintain a copy; earlier revisions of this document and of `em_api.py` embedded copies and they drifted.

```bash
# From the repo root:
adb push device/scripts/start_server.sh /sdcard/start_server.sh
adb shell "su -c 'cp /sdcard/start_server.sh /data/local/bin/start_server.sh && chmod 755 /data/local/bin/start_server.sh && chown root:root /data/local/bin/start_server.sh'"
```

> The script waits for `echoaudio` before starting — this ensures the audio DSP is initialised. `p2p0` is brought down to prevent mDNS interference. The WiFi wake lock prevents FireOS from suspending the wireless interface. All server output is logged to `/tmp/server.log` for debugging via `adb shell su -c 'cat /tmp/server.log'`.

> **Log cap (v2.7.1):** `/tmp` is RAM-backed and the script only ever appends — a background loop in the script checks every 5 minutes and, past 5MB, keeps the newest 512KB in `/tmp/server.log.1` and truncates `server.log` in place (the server's `O_APPEND` fd continues at the new EOF). Total log footprint stays bounded at ~5.5MB. A 45MB log was observed in the wild before this existed.

> The script runs the server as a subprocess (not via `exec`) so SIGTERM can be forwarded from Android init via the `trap`. If the binary exits in under 15 seconds three times in a row, the inactive A/B slot is restored via symlink and the script exits cleanly — init restarts it with the old binary. If the binary runs for ≥15s before crashing, the attempt counter resets (operational crash, not a deployment failure).

### Add EchoMuse and mixer service to the ramdisk:

The init scripts on FireOS 5 live in the boot image ramdisk. We need to unpack it, edit `init.csm.project.rc`, and repack.

Boot into TWRP:

```bash
adb reboot recovery
```

Extract magiskboot and unpack the boot image:

```bash
adb shell 'mkdir -p /tmp/work /tmp/bin'
adb shell 'unzip /sdcard/f1r30s.zip bin/magiskboot -d /tmp/'
adb shell 'chmod 755 /tmp/bin/magiskboot'
adb shell 'dd if=/dev/block/other-boot of=/tmp/work/boot.img bs=1048576'
adb shell 'cd /tmp/work && /tmp/bin/magiskboot unpack boot.img'
adb shell 'mkdir -p /tmp/ramdisk && cd /tmp/ramdisk && cpio -idv < /tmp/work/ramdisk.cpio 2>/dev/null | tail -3'
```

Pull the init script and edit it on your machine:

```bash
adb pull /tmp/ramdisk/init.csm.project.rc init.csm.project.rc
```

Append the following two service blocks to the end of `init.csm.project.rc`. The `mixer` stub must come first — EchoMuse's speaker Init() calls `stop mixer` as its first step:

```
service mixer /system/bin/sh
    oneshot
    disabled
    user root

service echomuse /data/local/bin/start_server.sh
    user root
    group root system
    class late_start
```

Push back, fix permissions, repack and flash:

```bash
adb push init.csm.project.rc /tmp/ramdisk/init.csm.project.rc
adb shell 'chmod 750 /tmp/ramdisk/init.csm.project.rc'
adb shell 'cd /tmp/ramdisk && find . | cpio -o -H newc > /tmp/work/ramdisk.cpio'
adb shell 'cd /tmp/work && /tmp/bin/magiskboot repack boot.img'
adb shell 'dd if=/tmp/work/new-boot.img of=/dev/block/other-boot bs=1048576'
adb reboot
```

### Verify:

After full boot (allow ~90 seconds):

```bash
adb shell "su -c 'getprop init.svc.echomuse'"
# Expected: running

adb shell "su -c 'cat /tmp/server.log'"
# Expected: Initializing... Ready... mDNS browsing...
```

---

---

## End state

After the wizard, an Echo is in one of two states:

- **emOS:** the boot partition holds emOS (our init and userspace around the
  Echo's own kernel), EchoMuse starts at boot, and Android never runs. The
  original boot image is saved by the wizard and restores from TWRP in about
  ten seconds.
- **FireOS with root:** FireOS 5 boots with SELinux permissive and Magisk
  root, the Alexa stack is disabled, and EchoMuse runs as an init service.

Either way the unlock and TWRP stay in place, and the Echo dials out to the
controller.

The feature-by-feature checklist that used to end this page described the
firmware as it was at v2.7 and had gone out of date. What each part does now
is in [configuration.md](configuration.md) and
[voice-pipeline.md](voice-pipeline.md), and how it got there is in the
[journal](../JOURNAL.md).
