# Persistent Root on the Amazon Echo Dot Gen 2 (biscuit)

*A complete guide to rooting, SELinux bypass, Alexa removal, EchoMuse installation, working speaker audio, VAD, wake word detection, and mute button — without tethered boot*

---

The Amazon Echo Dot 2nd Gen (codename: biscuit) has a small but dedicated hacking community. Most existing guides stop at tethered root — you get a root shell, but only while the device is connected to a computer running a patched preloader. Every reboot requires the cable.

This guide goes further. By combining the persistent amonet unlock with a boot image patch and a pre-seeded Magisk grant database, you get **persistent root that survives reboots** — no cable required after setup. Then we go further still and get EchoMuse running as a proper init service with full hardware access including working speaker audio.

At the end you'll have:
- Full root via Magisk 17.3
- SELinux in permissive mode
- Alexa voice stack completely disabled
- EchoMuse running on boot with full LED, mic, button, and speaker control
- Working audio output via TinyALSA directly (card 0, device 23)
- On-device energy VAD streaming speech bursts to the server over WebSocket
- OpenWakeWord wake word detection ("Hey Jarvis") on the centre/omni mic
- Directional mic selection — best perimeter mic locked for each voice turn
- Hardware mute button with LED feedback and action button lockout
- WiFi wake lock preventing FireOS from suspending the wireless interface
- Orange LED pulse while disconnected from server
- Two-plane WebSocket architecture (control + data) with no inbound ports

---

## Background & Credits

This builds on the work of:
- **R0rt1z2** — [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/) persistent unlock and TWRP
- **Dragon863** — [EchoCLI](https://github.com/Dragon863/EchoCLI) tethered root research
- **Binozo** — [GoTinyAlsa](https://github.com/Binozo/GoTinyAlsa) and original EchoGo SDK

The persistent unlock method (amonet-biscuit) is fundamentally different from the older tethered approach. EchoMuse replaces EchoGo with a WebSocket client architecture — no HTTP server on the device, no inbound ports, no ADB forward required for normal operation.

---

## Hardware

- Amazon Echo Dot 2nd Gen (RS03QR, 2016)
- Codename: biscuit
- SoC: MediaTek MT8163, quad-core ARM Cortex-A53 @ 1.5GHz
- RAM: 512MB
- OS: FireOS 5 (Android 5.1, API 22) or FireOS 6 (Android 7.2)
- MicroUSB cable required

---

## Prerequisites

- Linux or macOS machine with ADB and fastboot installed
- Python 3 (for boot image patching and Magisk DB creation)
- The following files downloaded and ready:
  - `amonet-biscuit-v1.1.0.zip` — from R0rt1z2's XDA thread
  - `update-kindle-csm_biscuit-272.6.8.0_user_680767620.bin` — FireOS 5 firmware
  - `f1r30s.zip` — ADB enablement patch
  - `Magisk-v17.3.zip` — from [GitHub](https://github.com/topjohnwu/Magisk/releases/tag/v17.3)
  - `server` — compiled EchoMuse binary (ARM, API 22)

> **Why Magisk 17.3?** Newer versions dropped support for Android 5.1 (API 22). 25.x installs but the daemon silently fails. 17.3 is the last version that works reliably on this device.

> **Linux ADB stability:** Linux aggressively power-manages USB devices by default, causing ADB disconnects. Disable autosuspend before starting: `echo -1 | sudo tee /sys/bus/usb/devices/*/power/autosuspend`. macOS doesn't have this problem.

---

## Step 1 — Update to FireOS 6.5.7.0

Before exploiting, the device must be on the latest FireOS 6.

On the device: **Settings → Device Options → About → Check for Updates**

Target: FireOS 6.5.7.0, build code `12383141252`. Keep updating until you land here.

> Once you're on the right version, work quickly — Amazon can push patches that break the exploit.

---

## Step 2 — Persistent Unlock, TWRP, and FireOS 5

Follow R0rt1z2's amonet-biscuit guide to install TWRP. This involves the kamakiri bootrom exploit and will modify your partition table — **it will wipe userdata**.

Once TWRP is running, flash FireOS 5 and the ADB patch:

```bash
adb shell twrp wipe data
adb shell twrp wipe cache
adb sideload update-kindle-csm_biscuit-272.6.8.0_user_680767620.bin
adb push f1r30s.zip /sdcard/
adb shell twrp install /sdcard/f1r30s.zip
adb reboot
```

The device should boot into FireOS 5 setup mode. ADB will be enabled.

---

## Step 3 — Patch the Boot Image for SELinux Permissive

This is the step that isn't documented anywhere else.

The Little Kernel (LK) bootloader hardcodes `androidboot.selinux=enforce` into the kernel command line — this is set before Android even loads, and it's what blocks every attempt to disable SELinux at runtime. You cannot `setenforce 0` as shell, you cannot `resetprop`, you cannot use `magiskpolicy`. The kernel won't let you.

The fix: we append `androidboot.selinux=permissive` to the boot image's own cmdline field. When both values are present in the kernel cmdline, permissive mode wins in practice on this device.

> **Note:** The `androidboot.selinux` value is a null-terminated ASCII string stored at a fixed offset (byte 64) in the Android boot image header, in a 512-byte field. We patch it directly rather than using magiskboot, which doesn't support cmdline modification on this version.

### From TWRP, extract magiskboot and pull the boot image:

```bash
adb shell 'mkdir -p /tmp/work /tmp/bin'
adb shell 'unzip /sdcard/f1r30s.zip bin/magiskboot -d /tmp/'
adb shell 'chmod 755 /tmp/bin/magiskboot'
adb shell 'dd if=/dev/block/other-boot of=/tmp/work/boot.img bs=1048576'
adb pull /tmp/work/boot.img boot_fresh.img
```

### Patch the cmdline on your Mac/Linux machine:

```python
python3 - <<'EOF'
with open('boot_fresh.img', 'rb') as f:
    data = bytearray(f.read())

cmdline_offset = 64
new_cmdline = b'bootopt=64S3,32N2,64N2 androidboot.selinux=permissive'

# Zero the full 512-byte field, then write new cmdline
data[cmdline_offset:cmdline_offset+512] = b'\x00' * 512
data[cmdline_offset:cmdline_offset+len(new_cmdline)] = new_cmdline

# Verify
print("New cmdline:", data[cmdline_offset:cmdline_offset+60])

with open('boot_patched.img', 'wb') as f:
    f.write(data)
print("Written to boot_patched.img")
EOF
```

Verify the output shows your new cmdline cleanly — no garbage bytes after `permissive`.

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

### On your Mac/Linux machine:

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

## Step 8 — Install EchoMuse

EchoMuse runs as a Go binary on the device. It abstracts the hardware (mic, speaker, LEDs, buttons) and connects outbound to the Clara server over two persistent WebSocket connections. There is no HTTP server on the device — no inbound ports, no iptables rules required.

### Set up the binary directory (A/B slots):

EchoMuse v2.4.4+ uses A/B slots: `server_a` and `server_b` with `/data/local/bin/server` as a symlink. This allows instant rollback without a binary transfer.

```bash
adb shell "su -c 'mkdir -p /data/local/bin'"
adb push server /sdcard/server
adb shell "su -c 'cp /sdcard/server /data/local/bin/server_a && chmod 755 /data/local/bin/server_a && ln -sf server_a /data/local/bin/server && chown root:root /data/local/bin/server_a'"
```

`server_b` starts empty. The first OTA update from the dashboard populates it.

### Create the startup script:

```bash
cat > /tmp/start_server.sh << 'EOF'
#!/system/bin/sh
# Wait for echoaudio (FireOS audio service) before starting
i=0
while [ $i -lt 120 ]; do
    pid=$(ps | grep echoaudio | grep -v grep)
    if [ -n "$pid" ]; then
        sleep 5
        break
    fi
    sleep 2
    i=$((i + 2))
done
ip link set p2p0 down
# Prevent FireOS from suspending the WiFi interface
echo "EchoMuse" > /sys/power/wake_lock
# Speaker mixer init
tinymix -D 0 56 On
tinymix -D 0 64 1 1
tinymix -D 0 88 On
tinymix -D 0 61 100 100
# Mic gain — equalised across all four ADCs for directional mic selection
# Values matched to Amazon's own initialisation (confirmed from firmware analysis)
tinymix -D 0 89 88 88
tinymix -D 0 92 40 40
tinymix -D 0 107 88 88
tinymix -D 0 110 40 40
tinymix -D 0 125 88 88
tinymix -D 0 128 40 40
tinymix -D 0 143 88 88
tinymix -D 0 146 40 40
kill $(ps | grep ledcontroller | grep -v grep)
exec /data/local/bin/server > /tmp/server.log 2>&1
EOF
adb push /tmp/start_server.sh /sdcard/start_server.sh
adb shell "su -c 'cp /sdcard/start_server.sh /data/local/bin/start_server.sh && chmod 755 /data/local/bin/start_server.sh && chown root:root /data/local/bin/start_server.sh'"
```

> The script waits for `echoaudio` before starting — this ensures the audio DSP is initialised. `p2p0` is brought down to prevent mDNS interference. The WiFi wake lock prevents FireOS from suspending the wireless interface. All server output is logged to `/tmp/server.log` for debugging via `adb shell su -c 'cat /tmp/server.log'`.

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

## Step 9 — Configure Audio for Speaker Playback

The ALSA mixer is initialised with incorrect defaults — the external speaker amp and DAC are disabled. Without fixing this, tinyplay will open the PCM device and hang silently. This is handled automatically by `start_server.sh`, but it's useful to understand and test independently.

### Understanding the audio hardware

The biscuit uses a MediaTek MT8163 SoC with a TLV320AIC32x4 external codec. Speaker playback goes through ALSA card 0, **device 23**, at 48kHz stereo S16_LE, period size 2048, period count 4.

The mixer has 239 controls. Three are wrong at boot:

| CTL | Name | Default | Required |
|-----|------|---------|----------|
| 5 | Ext_Speaker_Amp_Switch | Off | **On** |
| 56 | Audio_I2S1_Setting | Off | **On** |
| 64 | HP DAC Playback Switch | Off Off | **On On** |

### Test audio manually:

```bash
adb shell "su -c 'tinymix -D 0 5 On && tinymix -D 0 56 On && tinymix -D 0 64 1 1 && tinymix -D 0 61 100 100'"
```

Generate a test tone and play it:

```python
python3 - <<'EOF'
import struct, math
rate=48000; dur=2; freq=440
samples=[int(32767*math.sin(2*math.pi*freq*i/rate)) for i in range(rate*dur)]
stereo=[]
for s in samples: stereo.extend([s,s])
with open('/tmp/test48s.wav','wb') as f:
    f.write(b'RIFF')
    f.write(struct.pack('<I', 36+len(stereo)*2))
    f.write(b'WAVEfmt ')
    f.write(struct.pack('<IHHIIHH', 16, 1, 2, rate, rate*4, 4, 16))
    f.write(b'data')
    f.write(struct.pack('<I', len(stereo)*2))
    for s in stereo: f.write(struct.pack('<h', s))
print('done')
EOF
adb push /tmp/test48s.wav /data/local/tmp/test48s.wav
adb shell "su -c 'tinyplay /data/local/tmp/test48s.wav -D 0 -d 23 -p 2048 -n 4'"
```

You should hear a clean 440Hz tone.

---

## Step 10 — Server Setup

EchoMuse connects to the Clara server via mDNS discovery. The server must be running and advertising before the device boots (or the device will retry with exponential backoff until it finds it).

### mDNS advertisement

The Clara server advertises `_emcontroller._tcp.local` on port 8767. A dedicated `clara-mdns` container runs with `network_mode: host` to ensure multicast reaches the LAN.

**Proxmox note:** If running in a Proxmox LXC, the bridge requires the mDNS multicast MAC to be added manually:

```bash
# On Proxmox host
ip maddr add 01:00:5e:00:00:fb dev vmbr0
# Add to /etc/network/interfaces for persistence:
# post-up ip maddr add 01:00:5e:00:00:fb dev vmbr0
```

### Verify discovery from a Mac:

```bash
dns-sd -B _emcontroller._tcp local
# Expected: clara._emcontroller._tcp appears
```

---

## End State

```
✅ Persistent unlock (amonet-biscuit)
✅ TWRP installed
✅ FireOS 5 (Android 5.1)
✅ SELinux permissive — survives reboots
✅ Magisk 17.3 — persistent root, survives reboots
✅ Alexa voice stack disabled
✅ echoaudioservice retained (required for audio DSP init)
✅ EchoMuse running as init service on boot (exec mode, no crash loop)
✅ Dummy mixer service for EchoMuse init compatibility
✅ Audio mixer configured at boot (tinymix in start_server.sh)
✅ Mic gain equalised across all four ADCs — digital volume 88, MICPGA 40
✅ WiFi wake lock — FireOS cannot suspend wireless interface
✅ p2p0 (WiFi Direct) disabled — no mDNS interference
✅ Full LED ring RGB control (IS31FL3236A, 12 RGB LEDs)
✅ Microphone streaming (9 channels, S24_3LE, 16kHz, card 0 device 24)
✅ Speaker audio working (card 0, device 23, 48kHz stereo, period 2048 count 4)
✅ Button events (evdev)
✅ WiFi working
✅ Stable boot
✅ No HTTP server on device — no inbound ports, no iptables rules
✅ Three outbound WebSocket connections (control + data + shell planes)
✅ Device identity via ro.serialno — stable across reboots, matches adb devices
✅ Device approval flow — strict mode (pending) or auto mode
✅ Orange LED pulse while disconnected / searching for server
✅ Slow white LED pulse while pending controller approval
✅ On-device energy VAD — VAD end signal (0x04) sent to controller on silence
✅ Wake word detection on ch6 (centre/omni mic) — equidistant, no directional bias
✅ OpenWakeWord — "Hey Jarvis" detected server-side (threshold 0.3)
✅ Mic channel mapping confirmed empirically (tone injection, analyse_capture.py)
✅ Directional mic selection — best perimeter mic locked at voice turn start
✅ Direction estimation — onset ratio (fast/slow EWMA) robust to background noise (TV etc.)
✅ LED direction overlay — light green segment on listening ring during voice turn only
✅ LED mapping calibrated — LED 0 at 240°, confirmed from volume sweep
✅ Audio processing pipeline — RNNoise NS (vendored v0.1 C source, cgo) + AGC per period
✅ AGC release frozen during silence — prevents noise floor amplification past VAD threshold
✅ Acoustic feedback fix — controller sleeps for audio duration after EOS before mic restart
✅ Spinner runs for full response duration — duration calculated from PCM length
✅ VAD threshold lowered to 0.003 for comfortable conversational speech level
✅ Mute button — toggles mic mute, red LED ring, blocks action button
✅ Volume buttons — local interception, cyan LED ring feedback
✅ Amp boot click suppressed — mute/unmute around amp enable in pcm_speaker.go
✅ LED thinking spinner — triggered by THINKING signal from voice server
✅ Preroll discard — first frames of mic stream discarded to avoid wake word bleed-through
✅ Speech threshold — quiet recordings discarded without hitting Whisper
✅ OWW suppressed during speaker playback — prevents false wake triggers on own voice
✅ Stale mic queue drained after voice turn — prevents immediate re-trigger
✅ Config pushed from controller on connect — VAD/OWW params applied at runtime
✅ Device logs streamed to controller over control WebSocket
✅ Mute state change notifications — device sends mute_state message to controller
✅ Shell access — device dials outbound to controller on shell_open, no inbound ports
✅ OTA updates via controller dashboard — A/B slot system, local binary upload, instant rollback (symlink flip, no transfer)
✅ Auto-rollback on device — start_server.sh retries 3× before flipping to inactive slot; works without controller
✅ 8-band parametric EQ (controller-side, SVG frequency response curve, live updating)
✅ Wake word model hot-reload without device reconnect
✅ Hardware resource monitoring — CPU%, RAM, storage, WiFi RSSI every 30s; dashboard signal bars
✅ Voice server turn timeout (45s) — controller never hangs on unresponsive voice server
✅ Boot logging to /tmp/server.log
✅ mDNS via grandcat/zeroconf — RFC 6762/6763 compliant, reliable discovery
✅ WebSocket protocol keepalives — dead connections detected within 30s
✅ Controller management dashboard — React SPA, vendored assets, no CDN dependency
✅ Dashboard live state — mute/listen/speak/offline via WebSocket events + 5s poll
✅ Dashboard shell terminal — browser-based root shell, Ctrl+C support
✅ ESPHome native API satellite integration (VOICE_MODE=esphome)
✅ Both devices registered in HA as voice satellites (port 16001, 16002)
✅ ESPHome setup wizard passes on both devices
✅ TTS announcements via HA Assist pipeline (MP3→PCM via ffmpeg, standalone play)
✅ MediaPlayerState ANNOUNCING/IDLE transitions for wizard audio test
✅ ESPHome port lifecycle — ports up/down with physical device connect/disconnect
✅ mDNS _esphomelib._tcp per device (device_id[-12:] suffix to avoid prefix collision)
✅ DB migration v2 — esphome_api_port, esphome_noise_psk columns, next_esphome_port
✅ VOICE_MODE env var — claracore (default) or esphome, restart-to-apply
✅ OWW/button-triggered voice turns in esphome mode — full wake word → STT → intent → TTS → speaker round-trip confirmed working end-to-end against real HA Core 2026.6.4
✅ HA-side announce (setup wizard test, push TTS) plays correctly on device — live callback lookup, not a snapshot taken at connect
✅ Local no-speech timeout (5s) — matches Alexa's "wake word, then silence" behaviour; scoped correctly to bounded voice turns only, never the permanent OWW listening stream
```

**HA MVP reached** — this is the milestone ESPHOME_SPEC.md §1 called "the last functional barrier before a public v1 announcement." EchoMuse devices work as real Home Assistant voice satellites without ClaraCore.

---

## Mic Array Architecture

The biscuit has a 7-microphone array captured on ALSA card 0, device 24 as 9 channels S24_3LE at 16kHz. Ch7 and Ch8 are unconnected.

```
Ch0 → MK1 → 330°  (11 o'clock)  perimeter   ← confirmed empirically 2026-05
Ch1 → MK2 →  30°  ( 1 o'clock)  perimeter
Ch2 → MK3 →  90°  ( 3 o'clock)  perimeter
Ch3 → MK4 → 150°  ( 5 o'clock)  perimeter
Ch4 → MK5 → 210°  ( 7 o'clock)  perimeter
Ch5 → MK6 → 270°  ( 9 o'clock)  perimeter
Ch6 → MK7 → centre              omnidirectional
Ch7, Ch8 → unconnected
```

**Mapping confirmed** by tone injection testing (2026-05): phone speaker pressed against each mic hole in turn, per-channel RMS measured via `analyse_capture.py`. Previous documentation had Ch0/Ch1 swapped — corrected.

**ADC architecture:** Four TLV320ADC3101 stereo ADCs (I2C bus 0, addresses 0x18–0x1b). Probe order at boot determines channel assignment: 0x18→Ch0/1, 0x19→Ch2/3, 0x1a→Ch4/5, 0x1b→Ch6/7. All chips share a TDM data bus (confirmed from PCB trace analysis — DOUT shared, not daisy-chained). Array radius: 36mm (confirmed from PCB measurement).

**Why ch6 for wake word?** The centre mic is equidistant from all directions. OWW receives consistent audio regardless of where you're standing, and ambient sounds cannot lock it to a suboptimal direction. Perimeter mics are directional by proximity — good for STT once direction is known, but wrong for always-on wake word detection.

**Why directional mic selection for voice turns?** The mic physically closest to the speaker has the best SNR for that speaker. Selecting it at voice turn start (after wake word or button press) locks in the optimal channel for the duration of the turn. The lock happens at `mic_start` with `lock_mic: true` — not during ambient VAD activity — ensuring ambient sounds before the turn don't influence selection.

**Why mic selection rather than delay-and-sum?** At speech frequencies (<2kHz), a 72mm array has insufficient angular resolution to reliably discriminate between the 6 candidate directions. More critically, the maximum inter-mic delay is ~3.3 samples at 16kHz — requiring sub-sample fractional delay interpolation that introduces frequency-dependent phase errors causing comb filtering. Directional mic selection avoids all phase math and produces clean output.

**Frequency-domain beamforming** (implemented in `bf_capture` diagnostic tool): A frequency-domain delay-and-sum implementation exists applying exact phase shifts via FFT. Testing confirmed the approach works — flat spectral response, no interpolation artefacts. For voice pickup at typical conversational distances the SNR improvement over mic selection is marginal; mic selection remains the production path. The `bf_capture` tool is retained for future research.

**How Amazon does it:** Amazon's `amazon.speech.sim` reads the same raw 9-channel array via Android AudioRecord and does software processing. There is no hardware beamforming output channel. The MediaTek MAGI Conference DOA feature (in `audio.primary.mt8163.so`) is designed for phone call use cases and is not active in voice assistant mode on this device.

---

## Voice Pipeline

```
"Hey Jarvis"
    → on-device energy VAD (RMS ≥ 0.003, normalised, pre-AGC)
    → binary mic frames (ch6 omni, post-RNNoise NS + AGC) → /data WebSocket → server mic_queue
    → OpenWakeWord inference (hey_jarvis_v0.1, threshold 0.3)
    → wake detected
    → server: mic_stop
    → server: LEDs solid green (listening)
    → server: mic_start (lock_mic: true) → mic frames resume
    → device: direction estimation → locks best perimeter mic (highest onset ratio)
    → device: LED direction overlay on green ring (light green segment, beam-locked direction)
    → device: audio pipeline per period:
        raw 9ch S24_3LE → beamformer (locked mic extract) → RNNoise NS → AGC → S16_LE mono
    → device: VAD gate open (speech periods sent), silence dropped
    → device: speech ends → VAD gate closes → sends 0x04 (VAD end)
    → controller: 0x04 received → sends "END" to voice server
    → voice_server: END signal → sends THINKING → processes audio
    → server: LEDs spin green (thinking, direction overlay stops)
    → voice_server: Whisper large-v3 STT
    → Clara bot → response text
    → voice_server: Piper TTS (en_GB-alba-medium, 22050Hz)
    → server: resample 22050→48000Hz mono→stereo
    → server: device.speaking = True (OWW suppressed)
    → server: 0x02 binary frames → /data WebSocket → device ALSA
    → server: 0x03 EOS
    → server: sleep for audio duration (prevents acoustic feedback — speaker buffers ~341ms)
    → server: device.speaking = False
    → server: mic_stop → device unlocks perimeter mic, direction overlay clears
    → server: LEDs off
    → server: stale queue drain + model reset
    → server: mic_start (no lock_mic) → device returns to ch6 omni
    → OWW listening resumes
```

### ESPHome mode (VOICE_MODE=esphome)

```
"Hey Jarvis"
    → [same on-device path through OWW detection]
    → controller: VoiceAssistantRequest(start=True, flags=0) → HA Assist
    → mic audio streamed as VoiceAssistantAudio chunks to HA
    → VAD end → VoiceAssistantAudio(end=True)
    → HA: STT (Whisper) → intent → TTS
    → HA: VoiceAssistantAnnounceRequest(media_id=url, text="...")
    → controller: fetch MP3, decode via ffmpeg → 22050Hz mono S16_LE PCM
    → controller: EQ + resample 22050→48000Hz stereo → stream to device ALSA
    → controller: MediaPlayerState ANNOUNCING → AnnounceFinished → IDLE
    → LED off, mic restart
```

No-speech branch (device's 0x05 sentinel — see WebSocket Protocol below):
```
"Hey Jarvis" → [silence for 5s, nothing said]
    → device: 0x05 (no-speech timeout) instead of 0x04
    → controller: empty VoiceAssistantAudio(end=True) sent to close HA's
      already-open pipeline cleanly, but the 30s wait for a TTS response
      is skipped entirely — no HA round-trip result is awaited
    → turn ends quietly, mic restart
```

Action button triggers the same pipeline directly, bypassing wake word detection. Second press cancels at any stage.

---

## WebSocket Protocol

### Control plane (`ws://server:8767/control`) — JSON

Device → Server:
```json
{"type": "register", "device_id": "G0K0XXXXXXXX", "ip": "...", "version": "v2.3.0", "capabilities": [...]}
{"type": "button", "clickType": 138, "down": false}
{"type": "mute_state", "muted": true}
{"type": "log", "level": "info", "message": "..."}
{"type": "pong"}
```

Server → Device:
```json
{"type": "ack", "device_id": "G0K0XXXXXXXX"}
{"type": "pending"}
{"type": "config", "adcDigitalGain": 88, "adcMicpga": 40, "vadThreshold": 0.005, ...}
{"type": "leds", "leds": [{"id": 0, "r": 0, "g": 180, "b": 0}, ...]}
{"type": "mic_start"}
{"type": "mic_start", "lock_mic": true}
{"type": "mic_stop"}
{"type": "shell_open"}
{"type": "shell_close"}
{"type": "ping"}
```

### Data plane (`ws://server:8767/data`) — binary

Device → Server (mic frames):
```
[0x01][seq_hi][seq_lo][mono S16_LE PCM, 2560 bytes = 80ms]  — VAD-gated speech
[0x01][seq_hi][seq_lo][0x04]                                 — VAD end (speech detected, then ended)
[0x01][seq_hi][seq_lo][0x05]                                 — no-speech timeout (see below)
```
All three share the same `frameTypeMic` (`0x01`) wrapper and seq header — the VAD sentinels are single-byte *payloads*, not distinct top-level frame types. (0x02/0x03 below are genuinely distinct top-level types, speaker-direction only, no seq header — don't confuse the two framing conventions.)

**No-speech timeout (0x05), added v2.6.0.** `streamMic` (device/internal/client/data.go) only arms this when `lock_mic: true` was set on the `mic_start` that began the stream — i.e. only for a bounded voice turn (post-wake-word or button press), never for the permanent `lock_mic`-absent OWW listening stream. If no speech is ever detected (RMS never crosses `VadThreshold` for `VadSpeechMs` consecutive periods) within 5s of turn start, the device gives up locally and sends `0x05` instead of waiting on the existing silence-after-speech hysteresis, which never engages if speech never started. Distinguishing 0x05 from 0x04 lets the controller skip contacting HA's Assist pipeline entirely for a turn that never had anything to transcribe — mirrors Alexa's behaviour of quietly giving up on "wake word, then silence" rather than round-tripping to the backend just to receive `stt-no-text-recognized`. **This must never be armed for `lock_mic`-absent streams** — an earlier build armed it unconditionally, which silently killed the permanent wake-word listening stream 5s after every boot/reconnect with nothing to restart it (wake word "stopped working entirely," diagnosed via device log showing repeated `no speech detected within timeout` firing exactly 5s after every idle `Mic streaming started`, with no corresponding `mic_start` to revive it). Device-side test coverage (`internal/client/streamMic_test.go`) includes a regression test asserting a `lock_mic`-absent stream survives extended silence indefinitely.

Server → Device (speaker frames):
```
[0x02][stereo S16_LE PCM, 8192 bytes = one ALSA period]
[0x03] end of stream
```

### Shell plane (`ws://server:8767/shell/{device_id}`) — binary

Demand-opened by the Go binary dialling **outbound** to the controller on receipt of a `shell_open` control message. Raw stdin/stdout piped from `/system/bin/sh`. Single session enforced. The controller proxies this connection to the dashboard terminal. No inbound ports on the device.

---

## Connection Lifecycle

```
Device boots
  → orange LED pulse (searching for server)
  → mDNS browse: _emcontroller._tcp.local (grandcat/zeroconf)
  → connect /control → register (device_id = ro.serialno, version)

  CASE: unknown device, strict mode
    → server: sends {"type": "pending"}
    → device: slow white LED pulse — waiting for approval
    → device retries every 30s

  CASE: approved device
    → server: sends {"type": "ack"} + {"type": "config"}
    → device: applies config (tinymix for hardware params)
    → LEDs off (connected)
    → connect /data → identify
    → server: mic_start sent (no lock_mic — OWW mode)
    → device: mic streaming started on ch6 (centre/omni)
    → OWW listening (device shows IDLE state in dashboard)
```

If control drops → data cancelled → orange pulse resumes → both reconnect together on next mDNS discovery.
Controller detects dead connections within 30s via WebSocket protocol keepalives (ping 20s, timeout 10s).

---

## Key Files to Keep Safe

| File | Purpose |
|------|---------|
| `boot_patched.img` | SELinux-patched boot image |
| `magisk.db` | Pre-seeded root grant database |
| `Magisk-v17.3.zip` | Magisk installer |
| `f1r30s.zip` | ADB enablement patch |
| `update-kindle-csm_biscuit-272.6.8.0_user_680767620.bin` | FireOS 5 firmware |
| `server` | Compiled EchoMuse binary (ARM, API 22) — or fetch from GitHub releases |

If you need to reflash: Steps 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9. Your saved `boot_patched.img` already contains the SELinux patch — no need to repatch from scratch.

---

## Troubleshooting

### Device not connecting to server

```bash
adb shell su -c 'cat /tmp/server.log'
```

Common causes:
- **`mDNS: no server found`** — server not advertising. Check `dns-sd -B _emcontroller._tcp local` from Mac — should show `echomuse`.
- **White pulse, not orange** — device found the controller but hasn't been approved yet. Log into the management dashboard and approve the device.
- **`Connection lost: unexpected EOF`** — connecting to wrong server (stale mDNS cache). Another device on network may be advertising `_emcontroller._tcp`. Check `dns-sd -B _emcontroller._tcp local` from Mac.
- **p2p0 interference** — check `ip link show p2p0` on device — should be DOWN.

### No audio

```bash
adb shell su -c 'tinyplay /data/local/tmp/test48s.wav -D 0 -d 23 -p 2048 -n 4'
```

If this hangs: mixer not initialised. Run the tinymix commands from Step 9 manually.

### Mic not working / wake word not triggering

Check mic gain:
```bash
adb shell su -c 'tinymix -D 0 89'  # should be 88
adb shell su -c 'tinymix -D 0 92'  # should be 40
```

Wake word detection uses ch6 (centre/omni). VAD threshold is 0.005 normalised RMS — adjustable via config push from the dashboard. In noisy environments, raise to 0.010–0.020.

Check OWW model is loaded in controller logs — should see `OpenWakeWord model ready` on device connect.

### ADB not available after boot

```bash
adb shell su -c 'setprop persist.service.adb.enable 1'
adb shell su -c 'setprop persist.sys.usb.config mtp,adb'
adb shell su -c 'start adbd'
```

### Monitor active PCM devices

To see which processes own active ALSA devices in real time:

```bash
adb push pcm_watch.sh /data/local/tmp/pcm_watch.sh
adb shell su -c 'chmod 755 /data/local/tmp/pcm_watch.sh && /data/local/tmp/pcm_watch.sh'
```

`pcm_watch.sh`:
```sh
#!/system/bin/sh
while true; do
    for f in /proc/asound/card0/pcm*/sub0/status; do
        line=$(grep "owner_pid" "$f" 2>/dev/null)
        if [ -n "$line" ]; then
            pid=${line##*: }
            name=$(cat /proc/$pid/comm 2>/dev/null)
            state=$(grep "^state:" "$f")
            state=${state##*: }
            echo "$f pid=$pid state=$state -> $name"
        fi
    done
    sleep 2
done
```

---

## Audio Notes

**Why device 23?** The biscuit exposes 25+ PCM devices. Device 23 is the TLV320 DAC output path. Most other devices are modem/voice paths or internal DSP routes that hang or error on open.

**Why keep echoaudioservice?** The MediaTek audio DSP requires initialisation that happens inside Amazon's audio HAL (`audio.primary.mt8163.so`). Without `echoaudioservice` running, the I2S clock never starts and `tinyplay` hangs indefinitely. The service is a manifest stub — no Java code — its sole job is to trigger HAL initialisation via the Android audio framework.

**The mixer defaults are wrong.** Three mixer controls must be set after every boot — `start_server.sh` handles this automatically. Without them, tinyplay hangs silently on device 23.

**The dummy mixer service is required.** EchoMuse's speaker Init() calls `stop mixer` as its first step. Without a `mixer` service in init.rc, this call fails. Adding a dummy service allows `stop mixer` to succeed.

**Amp boot click suppression.** EchoMuse's `pcm_speaker.go` Init() mutes the output (tinymix ctl 61 → 0), enables the amp (ctl 5 On), waits 50ms for it to settle, then unmutes. This eliminates the click when the TPA3118D2 powers up.

**Mute implementation.** The mute button (KEY_MUTE, evdev code 113) arrives on `/dev/input/event1`. Mute is implemented by setting ADC_A Left/Right Mute (tinymix ctls 105 and 106). The mute controller intercepts the button locally, applies the tinymix change, updates the LED ring (red = muted), and signals the server to block dot button events.

**Mic gain — all four ADCs.** All four ADC pairs (A–D) are set to digital volume 88 and MICPGA 40. This matches Amazon's own initialisation values confirmed by analysing the unmodified device mixer state. Equalising all four ensures consistent sensitivity across all perimeter mics for directional selection.

**WiFi wake lock.** FireOS aggressively suspends the WiFi interface during inactivity, dropping WebSocket connections. Writing `"EchoMuse"` to `/sys/power/wake_lock` prevents this.

**Speaker streaming.** Audio is streamed as binary frames (8192 bytes = one ALSA period) over the data plane WebSocket. The device maintains a priority channel — the silence loop yields to real audio naturally, with backpressure at ALSA playback rate (~42ms/period). Piper TTS output (22050Hz mono) is resampled server-side to 48000Hz stereo before streaming.

**OWW threshold.** 0.3 works well for a London/Bristol accent — the default 0.5 is calibrated for American English.

**VAD threshold.** 0.005 normalised RMS is the default. Adjustable via config push from the dashboard — no rebuild required. In noisy environments (music, TV), raise to 0.015–0.020.

**VAD end signal.** When the device VAD gate closes (speech followed by `vadSilenceMs` of silence, default 600ms), the device sends a `0x04` binary frame. The controller forwards this as an `"END"` string to the voice server, which immediately processes the buffered audio. The device owns VAD state and signals it explicitly rather than the server inferring it from audio gaps.

**Directional mic locking — onset ratio.** When the controller sends `mic_start` with `lock_mic: true` (voice turn start), the device locks to the perimeter mic with the highest onset ratio: `energySmooth[di] / energyBaseline[di]`. This selects the direction with the biggest *recent energy increase* rather than highest absolute energy, making the lock robust to continuous background noise sources (TV, fan). Two parallel smoothers: fast (α=0.9, ~320ms) and slow (α=0.995, ~10s baseline). The slow baseline is frozen while locked. The lock is idempotent across VAD oscillation. Releases on `mic_stop`.

**Direction estimation — onset ratio.** Two parallel smoothers run per direction: fast (α=0.9, ~320ms) tracking instantaneous energy, and slow (α=0.995, ~10s) tracking the background noise floor. At lock time, the direction with the highest `energySmooth / energyBaseline` ratio is selected — this is the direction with the biggest *recent energy increase* (speech onset), not the direction with the highest absolute energy (TV, fan). The slow baseline is frozen during voice turns to prevent the speaker's own voice from corrupting the noise estimate. This reliably picks the speaker direction even with a television on in the room.

**LED direction overlay.** The direction arc is overlaid on the solid green listening ring during voice turns only (not during idle wake word listening). The overlay uses the controller-set base ring state rather than accumulating — each period resets to the base green and applies the direction marker fresh. Primary direction LED: bright light green (R:0 G:255 B:80). Adjacent LEDs: base green boosted by 60. The overlay stops immediately when the controller sends the thinking spinner (spinner LEDs are not solid green, so `listeningLEDs` flag goes false).

**LED physical mapping.** 12 LEDs (IS31FL3236A), one either side of each perimeter mic. LED 0 is physically at 240° (just clockwise of MK5 at 210°). Volume sweep confirmed: starts at LED 0, sweeps clockwise. Offset formula: `LED = ((angle - 240 + 360) % 360) / 30`.

**Audio processing pipeline.** Each 32ms period of raw beamformed audio passes through: (1) RNNoise noise suppression — vendored xiph/rnnoise v0.1 C source compiled via cgo into the Go binary, no external library required; 480-sample frame size handled via ring buffer against our 512-sample periods. (2) AGC — targets -22dBFS RMS with fast attack (0.05) and slow release (0.005); release frozen during silence to prevent noise floor amplification past the VAD threshold. VAD decision is made on pre-NS/AGC audio to keep threshold stable.

**Acoustic feedback prevention.** `stream_speaker` completes as soon as all frames are buffered on the device (~341ms ahead of actual playback). Without compensation, the mic restarts while the speaker is still playing, causing Clara to hear herself and trigger another voice turn. Fix: controller sleeps for `len(speaker_pcm) / (SPEAKER_RATE * 4)` seconds after streaming completes before calling `cleanup()`. The spinner continues running during this wait.

**Voice server turn timeout.** The controller waits a maximum of 45 seconds for the voice server to respond. Previously the controller would hang indefinitely if Whisper returned an empty transcription and the voice server closed silently. The timeout ensures the pipeline always resets cleanly.

**Stale queue drain.** After each voice turn, the mic queue is drained and the OWW model is reset before mic_start is sent. This prevents the device's own speaker output (buffered during playback) from immediately triggering another wake word detection.

**OWW suppression during playback.** While the speaker is streaming, OWW inference is suppressed server-side (`device.speaking` flag). The mic continues streaming (barge-in via button remains possible), but audio picked up from the speaker doesn't trigger false detections.

**mDNS library.** The `hashicorp/mdns` library fails to resolve the controller IP when python-zeroconf sends PTR responses with the A record under the hostname rather than the service name. Replaced with `grandcat/zeroconf` which is RFC 6762/6763 compliant and handles this correctly.

---

## What's Next

- **On-device wake word** — TFLite C binary running on-device, eliminating the continuous WiFi audio stream for OWW. OpenWakeWord has a TFLite backend; cross-compilation uses the existing echomuse-compiler Docker toolchain
- **PTY shell** — proper terminal emulator (top, vim, nano) via creack/pty + xterm.js in dashboard
- **Acoustic echo cancellation** — relevant once barge-in is implemented (speaker playing while mic active). Hardware AEC via MT8163 DSP is possible but complex; software AEC via speex or similar more practical.
- **Media player integration** — pause room audio on wake word, resume after response (Home Assistant `media_player` service call)
- **Bermuda BT proxy** — room-level presence detection via Bluetooth, once fleet of 5–6 Echo Dots is deployed
- **Adaptive VAD** — calibrate threshold on startup from ambient noise floor × multiplier. Currently fixed at 0.003; in very noisy environments this may need runtime adjustment.
- **RNNoise model upgrade** — vendored v0.1 model (2018). Newer models available via binary blob download; requires model loading API (rnnoise_model_from_file) present in newer source but needing the xiph.org CDN which was unavailable. v0.1 performs well for home environment use.
- **Startup chime** — short audio signature on EchoMuse init
- **Holding response** — play audio while Clara is thinking if response takes >2s
- **ESPHome native API satellite integration** ✅ — complete as of v2.6.0. Both devices registered in HA, voice turns working end-to-end.
- **Bidirectional volume control** ✅ — complete as of v2.6.1. Physical buttons, HA media player slider, and ALSA mixer all stay in sync. Survives controller and device restarts.
- **Continue-conversation without re-waking** — next up. `INTENT_END` already carries `continue_conversation` flag on every turn (confirmed in logs, currently discarded). Controller-only change — if flag is `'1'`, re-trigger `trigger_voice_turn()` after response finishes instead of returning to idle OWW listening. Zero device-side changes needed.

---

**Document version:** v2.6.1
**Last updated:** 2026-07-03
**Changelog:**
- v1.0 — April 2026: Initial publication. Full pipeline confirmed working.
- v1.1 — 2026-04-26: Fixed ambiguous init.csm.project.rc editing instruction; fixed `server &` → `exec` inconsistency.
- v1.2 — 2026-04-26: Updated start_server.sh; added VAD stream, OpenWakeWord, mute button, amp click suppression; updated end state.
- v1.3 — 2026-04-27: Added THINKING signal, preroll discard, speech threshold, mDNS conflict handling, OWW model loading notes.
- v2.0 — 2026-05-09: Major architecture update. EchoGo replaced by EchoMuse. HTTP server removed from device entirely. Two-plane WebSocket architecture (control + data). gorilla/websocket replacing golang.org/x/net/websocket. p2p0 disable added. Proxmox bridge multicast fix documented. Orange disconnect LED pulse. OWW suppression during playback. Stale queue drain. Boot logging. Updated voice pipeline, end state, troubleshooting, and all file references.
- v2.1 — 2026-05-19: Device ID changed to ro.serialno. Version embedded via ldflags. Three-plane WebSocket (added /shell). Device approval flow (strict/auto modes, pending white pulse). Config push on connect. Device log streaming. VAD end signal (0x04 frame type) replaces server-side silence detection. OWW model download at build time. mDNS library replaced with grandcat/zeroconf. Controller management dashboard (port 8768, auth, DB, API, GitHub release tracking, OTA updates).
- v2.2 — 2026-05-20: Shell architecture corrected — device dials outbound to controller on shell_open, no inbound ports on device. Mute state tracking via mute_state control message. Dashboard live state updates via WebSocket events (mute/listen/speak/offline). WebSocket protocol keepalives — dead connection detection within 30s. Dashboard React SPA compiled via esbuild, fully vendored assets (no CDN). Ctrl+C support in browser terminal.
- v2.3 — 2026-05-25: Mic array architecture overhaul. Wake word detection moved to ch6 (centre/omni) for direction-independent reliability. Directional mic selection — best perimeter mic locked at voice turn start via `mic_start` with `lock_mic: true`, released on `mic_stop`. Lock is idempotent across VAD oscillation. Mic gain equalised across all four ADCs (88/40) matching Amazon's initialisation values. Voice server turn timeout added (45s). pcm_watch.sh diagnostic added. Hardware audio investigation documented — confirmed software-only processing, no hardware beamforming output channel on this device.
- v2.4 — 2026-05-28: Mic channel mapping corrected (Ch0=MK1=330°, Ch1=MK2=30° — previous docs had these swapped; confirmed by tone injection testing). ADC architecture documented (four TLV320ADC3101, I2C 0x18–0x1b, TDM shared bus, array radius 36mm). Direction estimation upgraded to onset ratio (fast/slow EWMA) — robust to continuous background noise sources. Audio processing pipeline added: RNNoise noise suppression (vendored xiph/rnnoise v0.1 via cgo, no external dependencies) + AGC with speech-gated release. VAD threshold lowered to 0.003 for comfortable conversational level. Acoustic feedback bug fixed — controller sleeps for audio duration after streaming. Spinner duration fixed — runs until audio playback truly completes. LED direction overlay redesigned — light green segment on listening ring, shows during voice turns only, stops when spinner starts. LED physical mapping calibrated (LED 0 at 240°). `listeningLEDs` flag gates direction overlay to prevent interference with spinner/other animations. bf_capture diagnostic tool documented. Voice server END handler hardened — THINKING send failure no longer silently drops transcription.
- v2.4.1 — 2026-06-13: Stability and correctness pass. Connection lifecycle: pong ticker goroutine leak fixed (done channel tied to connection lifetime); data-plane reconnects independently on /data drop without waiting for /control to cycle; register message sent before conn published to prevent concurrent write race; controller device registry guarded with identity checks to prevent reconnect races on handler teardown; per-device OWW model instances replace shared singleton (thread-safety, state isolation); mDNS refresh loop implemented fixing silent IGMP keepalive regression on Proxmox bridge; mdns_task NameError on shutdown fixed. Audio recovery: speaker silenceLoop death no longer causes PumpPeriod to block forever (deadCh); mic ALSA stream death closes subscriber channels so streamMic exits cleanly; streamMic defer resets micActive on exit from any cause. Concurrency: LED i2c writes serialised with mutex; SQLite write transactions serialised with threading.Lock; beamformer Unlock moved to mic goroutine (eliminates data race with Process). Beamformer: fixed-beam steerAngle now applied in Process() — nearestDirection was implemented but never called; Lock() uses raw energy during 3s baseline warmup rather than meaningless onset ratios; hfEnergy direction index corrected. Mute: physical mute button is now device-sovereign — controller mic_start refused when muted; mute state reported on every reconnect; red ring restored after orange pulse on reconnect; OWW detection suppressed and buffer cleared when muted. Controller: resample rewritten with numpy (~1-2s blocking loop replaced with <5ms); TTS tail padding prevents last ~42ms of audio being dropped; OWW threshold updates live without reconnect; spinner overshoot fixed — sleep tracks elapsed streaming time and waits only remaining playback duration.
- v2.4.2 — 2026-06-13: Correctness fixes and HTTP layer removal. Small fixes: handle_shell task leak plugged (tasks now cancelled in finally regardless of asyncio.wait outcome); VAD-end sentinel no longer silently dropped when queue full — drains one audio frame to make room rather than losing the end-of-speech signal (previously caused voice turns to hang until 45s timeout); BeamAngle field changed to *float64 so 0° is distinguishable from absent-from-message. HTTP rip-out: gin stack, all HTTP handler files, and pkg HTTP client wrappers removed — the HTTP server was never started (Serve() was wired but never called since v2.0); volume_buttons.sh removed (was stuck in a curl wait loop since the HTTP endpoint never existed; volume buttons handled by Go binary via evdev throughout). go.mod cleaned of gin and 15 exclusive transitive dependencies; binary size reduced accordingly. Run `go mod tidy` inside the compiler container after checkout if go.sum needs regenerating.
- v2.4.3 — 2026-06-13: RNNoise VAD probability, shell console fix, version embedding. RNNoise: ProcessFrame return value (speech probability 0–1) was previously discarded on every call; now stored in Processor and used to gate AGC release — if RNNoise confidence < 0.5, AGC release freezes even when RMS is above stream VAD threshold, preventing gain pumping on loud non-speech (TV, HVAC). Stream VAD gate remains RMS-only; vadHasData flag prevents incorrect gating during the ~30ms startup window before first RNNoise frame. Shell console: inner proxy functions (device_to_dashboard, dashboard_to_device) were accidentally removed in the v2.4.2 handle_shell task-leak fix, causing NameError on every shell connection attempt; restored. Version embedding: compile.sh now uses --entrypoint bash with explicit -ldflags to embed the git tag (or datetime-dev for dirty trees) into the binary at compile time; devices now report their running version to the controller dashboard correctly rather than always showing "dev".
- v2.4.4 — 2026-06-14: EQ, stats, A/B OTA, shell fixes. Output codec documented: TLV320AIC32x4 on I2C bus 2 (separate from the 4x TLV320ADC3101 input chips); hardware biquad EQ (117-byte ALSA control) identified and decoded but software EQ chosen for flexibility. 8-band parametric EQ implemented controller-side using Audio EQ Cookbook biquad formulas (low shelf 125Hz, peaking Q=1.4, high shelf 8kHz); SVG frequency response curve in dashboard updates live as sliders move; 2-column layout with Flat/Clarity/Warmth preset buttons and loudness toggle. WiFi RSSI offset-encoding fix: /proc/net/wireless on this kernel encodes level as positive offset (0-255); values > 0 corrected by subtracting 256 (e.g. 206 -> -50 dBm). Device stats reporting: new stats.go in device/internal/client sends CPU%, RAM, storage, WiFi RSSI to controller every 30s and on connect; status tab redesigned with resource bars and 4-bar WiFi signal indicator. Wake word model hot-reload: owwModel config changes reload the model in the running wake_word_listener without device reconnect. A/B binary update system: start_server.sh rewritten with 3-attempt retry loop (15s minimum runtime threshold), SIGTERM trap, and auto-rollback via symlink flip before clean exit; controller OTA detects/migrates legacy layout in one shell session, streams binary to inactive slot, atomically flips symlink and restarts; rollback is instant symlink flip only; local binary upload (POST /api/releases/upload) with file picker in dashboard. Shell/console fixes: @auth.require_admin removed from _ws_shell (was rejecting WebSocket upgrades — browsers cannot set Authorization headers; auth handled by ws_resolve_session via ?token= query param); programmatic shell race fixed using ws.wait_closed() + per-device asyncio.Lock; _shell_pending cleanup moved exclusively to _release_shell_ws. OTA implementation fixes (all discovered against device): binary transfer uses shell heredoc piped to `busybox base64 -d` — printf and base64 are not in PATH on FireOS mksh, only available as BusyBox applets; decoder auto-detected at transfer time (busybox base64 → python3 → python); service restart uses `kill $PPID` from within the shell session rather than `stop/start echogo` — stop echogo kills the entire Android cgroup including the shell (child of server process), so start echogo never ran; kill $PPID sends SIGTERM to the server binary and start_server.sh's wait loop restarts cleanly from the updated symlink. Controller OTA fixes: `str(msg)` → `msg.decode('utf-8')` in _shell_run (device sends WebSocket binary frames; str() produced repr `b'SLOT:server_a\n'` as a literal string, breaking slot detection); duplicate _get_device_shell_ws/_stream_binary_via_shell/_exec_shell definitions removed (shadowed correct implementations in Python); _ws_shell now checks _shell_lock before opening interactive console (opening terminal mid-transfer previously overwrote _shell_pending, cancelled device shell context, and killed the transfer); _extract_binary_version() scans raw binary for embedded version string pattern (20\d{6}-\d{4}-[a-z]+) so local uploads report the binary's own version rather than a controller-generated local-YYYYMMDD-HHMM label; _monitor_reconnect accepts version != previous_version as success (covers local uploads where binary self-reports its own string); TRANSFER_OK wait extended to 120s; reconnect initial sleep 8s.
- v2.4.5 — 2026-06-18: Dashboard provisioning wizard, first pass. Browser-side ADB client over WebUSB (Chrome/Edge only; no server-side ADB required) — initial implementation was hand-rolled: RSA auth via BigInt modular exponentiation (Web Crypto always pre-hashes, so the PKCS#1 v1.5 pad + private RSA op was done manually), binary file push via `exec:cat >`, binary pull via `exec:cat`. Dashboard gains an admin-only "+" tile in the device grid that opens an 11-step wizard: (0) connect device in Android mode, verify FireOS 5 build, `adb reboot recovery`; (1) reconnect to TWRP; (2) patch boot image — SELinux cmdline (bytes 64–575 zeroed, `androidboot.selinux=permissive` written) and init.rc service entries combined into a single magiskboot unpack/repack cycle; (3) flash Magisk 17.3 via `twrp install`; (4) push pre-seeded magisk.db (generated on-the-fly by controller via `GET /api/provision/magisk_db`, grants uid 2000 and uid 0 always-allow); (5) reboot to Android; (6) reconnect; (7) verify root; (8) configure WiFi — pushes wpa_cli helper script to avoid shell quoting, polls for IP; (9) disable all 9 Alexa voice stack packages; (10) push server binary (A slot) + startup script (`GET /api/provision/start_script`). Two new admin-only controller endpoints added: `GET /api/provision/start_script` and `GET /api/provision/magisk_db`.
  **Superseded in v2.5.0** — the hand-rolled ADB client never worked reliably end-to-end (USB interface claim/reconnect hangs, RSA auth edge cases) and was replaced entirely with the `@yume-chan/adb` library. See v2.5.0 for the working implementation.
- v2.5.0 — 2026-06-20: Provisioning wizard rewritten on a proven ADB library; full WiFi configuration root-caused and fixed end-to-end on hardware. This was a long, evidence-heavy session — the entries below are deliberately detailed since most of what was found here is non-obvious and easy to silently regress.
  **ADB client replaced.** The v2.4.5 hand-rolled WebUSB ADB client (manual CRC32, BigInt RSA, raw USB packet framing) never worked reliably and is gone. Replaced with `@yume-chan/adb@2.1.0` + `@yume-chan/adb-daemon-webusb@2.1.0`, lazy-loaded from esm.sh at runtime (works fine under esbuild `--bundle=false` since dynamic `import()` of a URL passes through untouched). Auth uses the library's own `ADB_DEFAULT_AUTHENTICATORS` — the separate `@yume-chan/adb-credential-web` package was tried first but its only export is `default`, not a usable credential store class; dropped entirely, not needed. Shell commands use `adb.subprocess.noneProtocol.spawn()` (Android 5.1 requires `noneProtocol` — `shellProtocol` needs Android 7+). File push/pull goes through `cat > path` / `cat path` over the same spawn mechanism; push does **not** drain the process's stdout after closing stdin — busybox `cat` on TWRP never closes stdout when stdin closes, so draining hangs forever. Reconnecting to a device the browser already has a USB handle for (e.g. retry after a reboot) hangs indefinitely unless the previous `AdbDaemonWebUsbDevice.disconnect()` is called first — the wizard now tracks the last-connected USB device handle and disconnects it before requesting a new one.
  **TWRP detection** changed from `getprop ro.bootmode` / `ls /sbin/recovery` (both unreliable on this TWRP build) to checking the ADB banner product name directly — TWRP self-identifies as `omni_biscuit`, Android as `csm_biscuit`; this is exposed as a public `Client.banner` property.
  **Device picker naming corrected**: Android-mode connections show as **"AEOBC"** in the browser's USB device picker, TWRP-mode connections show as **"Echo"** — the wizard's step descriptions previously had this backwards.
  **Duplicate-device guard added**: step 0 now reads `ro.serialno` and cross-checks it against the device list already loaded in the dashboard before proceeding into the destructive TWRP/wipe flow; throws with a "delete from controller" action if a match is found. Field name for the controller's device-serial comparison is matched defensively (`serial`/`serial_number`/`device_id`/`id`) since the exact schema wasn't confirmed against `em_api.py` at time of writing.
  **Step resumability**: sidebar steps are now clickable to jump to any non-running step, so an aborted provision can be re-entered without restarting from step 0.
  **`su -c` argument handling — load-bearing finding, do not regress.** `su -c` on this device's `su` binary only correctly passes through its command if everything after `-c` is **one single-quoted argument**. `su -c echo "test \"quoted\" value"` (multiple words) silently mangles/drops the quoted portion; `su -c 'echo "test \"quoted\" value"'` (one argument) works correctly. This was the root cause of an entire afternoon of `wpa_cli set_network` quoting failures before it was isolated. Related: `su -c` appears to give each `;`-separated statement inside that single argument its **own shell instance** — `su -c 'x="hello"; echo "got: $x"'` prints `got: ` with the variable empty, because `echo` runs in a different shell than the one that set `x`. Pipelines (`ps | grep ... | while read ...; do ...; done`) do **not** have this problem since they're a single shell construct, not sequential statements — use pipelines, not `;`-separated sequential assignment, for any `su -c` command that needs to pass a value between steps.
  **mksh shell redirects (`>`/`>>`) are unreliable on this device for arbitrary content writes** — confirmed via `printf`/`tee -a`/bare `>` all failing identically with `can't create ... Permission denied` on files and directories with completely correct ownership, mode, and SELinux context (this consumed a large part of the session chasing permissions/SELinux/quota theories before the actual pattern was found). `cp` and `tee` **without** `-a` (create/truncate mode) both work reliably; append-mode opens (`>>`, `tee -a`) do not. The wizard's WiFi config writer works around this by building the full file content in JS, base64-encoding it, decoding via `busybox base64 -d | busybox tee <path>` (never a raw redirect), then `cp`-ing into place.
  **`chmod 666` on a directory breaks it** — stripping the execute/traverse bit on `/data/misc/wifi` (done while debugging file permissions) made every file inside unopenable by any process, including ones with correct individual file permissions, for hours, while every diagnostic pointed at the file rather than the directory. Restore to `770`. The wizard now does this defensively before every WiFi config write.
  **`wpa_cli` on this build (v2.3-5.1.1) requires both `-p <socket dir>` and `-i <iface>` explicitly** for every non-interactive invocation — `ctrl_interface=/data/misc/wifi/sockets` in the config is non-default, so `wpa_cli` without `-p` either fails outright (`Failed to connect to non-global ctrl_ifname`) or, once other client sockets exist in that directory from unrelated processes, mis-selects one of those instead and fails with `Operation not permitted`. `IFNAME=wlan0` as a bare leading argument is **not** valid syntax on this `wpa_cli` build (despite appearing to work for `add_network`/`scan` by accident, since those happened to fall back to the only non-p2p interface) and fails outright for `status`/`list_networks`. Always use `wpa_cli -p /data/misc/wifi/sockets -i wlan0 <command>`.
  **Two independent `wpa_supplicant` processes can run simultaneously and fight over `wlan0` — this was the actual root cause of WiFi config changes silently reverting.** The bare init service (controlled by `start`/`stop wpa_supplicant`) launches a minimal instance with no p2p, no overlay config, no Android control socket. `svc wifi enable` independently launches the **proper** Android-framework-managed instance (`wlan0` + `p2p0`, overlay configs, entropy file, `-g@android:wpa_wlan0` abstract socket for `WifiStateMachine`/`WifiNative`). If both end up running — e.g. because `svc wifi enable` was called once, then the wizard separately `kill -9`'d and `start`'d the bare service — they contend for the same netdev; the framework instance reasserts whatever network it already knows about via its own saved profile, writes it back to `wpa_supplicant.conf` (since `update_config=1`), and the bare instance's `wpa_state` degrades from `DISCONNECTED` to `INTERFACE_DISABLED` and never recovers. **`stop wpa_supplicant` does not reliably kill the bare-service process either** — it can flip `init.svc.wpa_supplicant` to `stopped` while the old process keeps running untouched, serving stale config indefinitely; `start` afterward then silently no-ops or fails because the old process still holds the control sockets.
  **Fix, and the only WiFi reload mechanism the wizard now uses:** write the config file, then `svc wifi disable` followed by `svc wifi enable`. This manages the proper framework instance exclusively, and on this device it auto-associates and obtains a DHCP lease via the framework's own handling with **no manual `wpa_cli reconnect` and no manual `dhcpcd` invocation needed** — both were required workarounds in earlier iterations of this fix and are now actively wrong, since they encourage standing up the conflicting bare-service path. `runConfigWifi` asserts exactly one `wpa_supplicant` process is running after reload and throws if it finds more than one, so any regression of this fails loudly rather than silently reverting again.
  **`com.amazon.android.service.wifiprofilemanager` and `com.amazon.device.smarthome.adapters.wifi` both interfere with manual WiFi configuration** and are now disabled alongside the original 9 Alexa packages in `runDisableAlexa` (12 packages total). `wifiprofilemanager` re-asserts its own saved network profile through the framework `WifiManager` path (pure Java/Dalvik — `classes.dex` only, no native binary, no shell script; it talks to wpa_supplicant via framework IPC, not anything greppable on disk). `pm disable` on the smarthome wifi adapter package does **not** stop the native `/system/bin/SmartHomeWifid` binary — it's launched directly by `/init.smarthome.rc` via a property-trigger chain (`persist.wifi.migrate.complete=1` → `wifi.launch` reaches `111` → `start smarthomewifid`), independent of the Android package manager. Clearing `setprop persist.wifi.migrate.complete 0` (a `persist.` property, survives reboots) durably prevents the chain from ever reaching `111`, so `SmartHomeWifid` never starts on subsequent boots; `runDisableAlexa` also kills it directly if it's already running in the current boot.
  **Package manager not ready immediately after `su -c id` succeeds.** Root/Magisk being up does not mean the Android framework has finished booting — the first several `pm disable` calls in a fresh-boot wizard run can fail with `Could not access the Package Manager. Is the system running?` before later calls in the same loop start succeeding. `runDisableAlexa` now polls `getprop sys.boot_completed` (up to 30s) before the disable loop, plus a one-shot 3s-delay retry on any individual `pm disable` call that still hits the error.
  **WiFi scan** (`wpa_cli -p ... -i wlan0 scan` / `scan_results`) now feeds a network picker UI with signal-strength sorting and manual SSID/password entry as a fallback; config write is single-network-only (deliberately drops any prior Alexa-era saved network rather than risk silent fallback to it).
  **Other fixes**: `awk`/`cut`/`which`/`head` are all absent on this image (only discovered when a wizard run failed mid-PID-extraction) — every PID/field extraction in the wizard now uses `ps | grep ... | while read user pid rest; do echo $pid; done` style pipelines instead, which need no external tool. EchoMuse install step (was: file upload only) now offers "install latest from GitHub" alongside a custom binary upload — the controller endpoint for this (`/api/provision/latest_binary`) is a naming guess based on the dashboard's existing `/api/devices/{id}/...` convention and the project's known `release_poll_loop()` mechanism, **not yet confirmed against `em_api.py`**. Provisioning now ends with an explicit device reboot and a clear "Done" screen instead of leaving the wizard sitting at the last step.
  **Still open**: `/api/provision/latest_binary` and the device-delete route used by the duplicate-device guard are both unconfirmed against `em_api.py` source (the delete route worked once in testing; the latest-binary route 404'd and needs the real endpoint name).
- v2.5.1 — 2026-06-20: Wizard hardening pass — every "still open" item from v2.5.0 closed out, plus several silent-failure classes found and fixed on real hardware. Recurring theme this session: code that assumed a fresh, never-provisioned device, and silently inherited stale state on a re-flash instead of failing loudly.
  **`/api/provision/latest_binary` confirmed and implemented.** No such route existed in `em_api.py` — the v2.4.5/v2.5.0 naming guess 404'd as expected. `GET /api/releases/latest` exists but only returns release metadata (`{version, url}`), not the binary itself, and the fleet OTA path (`/api/devices/{id}/update`) requires a live WebSocket session a freshly-flashed, not-yet-registered device doesn't have. New route reuses the existing `_get_cached_release()`/`_fetch_binary()` machinery and streams the binary directly, with the release version returned in an `X-Release-Version` header so the wizard log can show what was actually fetched.
  **Duplicate-device field matching confirmed and simplified.** `_merge_device()` in `em_api.py` confirms `device_id` is the only identifying field on a device object — it **is** `ro.serialno` (set at registration in `em_controller.py`), not a separate `serial`/`serial_number`/`id` field. The v2.5.0 defensive multi-field guess is gone; the duplicate check now matches on `device_id` alone.
  **Delete-while-connected dashboard hang root-caused and fixed.** Deleting a device from the controller while its duplicate-detection ADB session was still open caused the dashboard to hang at "Authenticating ADB…" on retry, requiring a page reload. Root cause was client-side, not in `em_api.py`: the duplicate-detection throw path never called `c.close()`/`setAdb(null)` on the already-authenticated ADB session before throwing, unlike the clean-exit path a few lines below it. The live transport stayed open and `_lastUsbDevice` kept pointing at it; the next `requestDevice()` call disconnected the stale WebUSB interface claim but never told the still-open ADB transport to close, so the following `Transport.authenticate()` raced a half-torn-down session. Fixed by mirroring the clean-exit teardown on the duplicate-detection throw path. `DELETE /api/devices/{id}` itself was confirmed correct in the same pass — it only touches the DB row, no live WebSocket teardown, which is consistent with the bug being entirely client-side.
  **Wizard navigation reworked — strictly linear, no jump-back, retry-with-different-input added.** Sidebar step list is now a read-only progress indicator (no click handlers) rather than letting the user jump to any `done`/`error`/`pending` step, since jumping back didn't reset downstream step state and there was no way to get the device into the right boot state (TWRP vs Android) for a backward jump anyway. In exchange, retrying a failed step now actually lets you change what you're retrying with: the Magisk-zip and EchoMuse-binary file pickers are now gated on `stepState !== 'done'` instead of `=== 'pending'` (matching the existing WiFi panel pattern), so a failed attempt re-shows the file input instead of vanishing behind a generic "Retry" button that would silently reuse the same — possibly wrong — file. The file selection is also explicitly cleared on error, forcing a deliberate reselect. The generic "Retry" button is now suppressed on steps that have their own dedicated retry UI (Magisk, EchoMuse binary, WiFi), since both rendering at once meant retry-with-no-file-selected was reachable as a confusing second failure mode.
  **EchoMuse install step rewritten after a real-world failure: GitHub-install button was silently keeping a stale binary in place instead of installing the new one.** The original install command chained `mkdir && cp && chmod && ln -sf` with no stderr capture and no result check — a silent `cp`/`ln` failure anywhere in the chain (this device had a stale `server_a`/`server_b`/`server` from a prior OTA-managed install, surviving a wizard re-flash since `/data` isn't wiped by a boot-image patch) would short-circuit the chain before the symlink flip ran, and the wizard logged "EchoMuse installed" regardless. Fixed in two parts: (1) `server`, `server_a`, and `server_b` are now explicitly `rm -f`'d before every install, so a re-provisioned device can't inherit a stale OTA-managed binary state that the wizard's own install logic never accounted for; (2) every step of the actual install (`cp`, `chmod`, `ln -sf`) now runs as a separate `2>&1`-captured call with its output logged, and the result is verified afterward via `readlink` (must report `server_a`) and a `c.pull()` byte-count check against the pushed binary, rather than trusting the chained command's silence as success.
  **Magisk preseed step rewritten after a real-world ~multi-minute hang traced to `magiskd` hard-rejecting every `su` call.** On a re-flash of a previously-rooted device, `su -c id` took up to ~60s per attempt before being rejected — confirmed via on-device `magisk.log`: `sqlite3_exec: no such table: settings` / `strings` / `policies` on every request, followed by `su: request rejected (2000->0)`. Two compounding causes, both fixed: (1) `_get_provision_magisk_db` in `em_api.py` only ever created a `policies` table, with the wrong schema (extraneous `package_name` column, no primary key) — corrected to match Magisk v17.3's actual schema (`policies` keyed on `uid` alone, plus `settings`, `strings`, and `denylist` tables), confirmed against a real working device's `sqlite3 .schema` dump rather than guessed. (2) This alone doesn't explain the failure, since the same incomplete schema has worked across many prior **fresh**-device provisions — magiskd's own first-boot startup appears to migrate/complete an otherwise-valid-but-incomplete `magisk.db` itself, as long as nothing else interferes. On a re-flash, `/data/adb/magisk.img` (Magisk's own module/data image, entirely separate from `magisk.db`, never touched by this wizard) survives from the prior install — boot-image patching and a TWRP Magisk re-flash don't wipe `/data`. `magisk.img` gets merged/mounted at `post-fs-data`, before any `su` request is handled; stale state there plausibly disrupted magiskd's normal DB-migration path on this boot. `runPreseedDb` now `rm -f`s both `magisk.db` and `magisk.img` before pushing the fresh DB — scoped to those two files specifically, not the whole `/data/adb` directory, since TWRP's Magisk zip install (the immediately preceding step) writes Magisk's own binaries and script directories under there too. This step runs in the TWRP shell session (no reconnect happens between Magisk install and preseed), so the clear uses a plain `rm`, not `su -c rm` — there is no `su`/magiskd to broker through yet at this point in the sequence.
  **`readlink` on this device prints an error message on a missing target rather than returning empty output** — discovered when a new `2>&1`-captured `readlink` check (added for the EchoMuse install verification above) broke the existing, working pattern of treating empty `readlink` output as "symlink absent." The existing OTA code in `em_api.py` already worked around this correctly by using `2>/dev/null`; the new wizard-side checks were initially written with `2>&1` for visibility and had to be corrected to match. A planned third verification step (confirming `server_a`/`server_b` are actually gone via `c.pull()`/`cat` after `rm`) was dropped entirely rather than risk the same failure mode with an unverified command — `cat`'s missing-file behavior on this device's shell was never directly tested, and a verification step that false-aborts a working clear is worse than no verification at all.
  **Device rename and delete added to the dashboard's device detail modal**, both admin-gated to match the existing `@auth.require_admin` on `PATCH`/`DELETE /api/devices/{id}`. Rename is inline (click the device name, edit, Enter/Save or Escape/Cancel); delete requires a two-step confirm given it's unrecoverable from the UI. Neither needed new client-side state-sync logic — both routes already broadcast events (`device_update`, `device_deleted`) over the existing `/api/events` WebSocket that the dashboard already merges into device state.
  **Force release-check (`POST /api/releases/check`) wired up — existed server-side, nothing called it.** `_get_cached_release()` serves a 60s in-memory cache, falling back to a DB cache that's only re-polled in the background once it's older than `update_check_interval` (default 1h) — both the Updates tab and the wizard's GitHub-install step only ever read that cache, so neither could surface a release published in the last hour without waiting. `_post_check_release` (force-poll, bypasses both caches) already existed and worked but had no UI calling it. Added a "Check now" button to the Updates tab and a "Check for newer release" button to the wizard's GitHub-install step (shown inline before committing to install, doesn't change what gets installed — that still reads the now-freshly-populated cache).
  **"Deploy all" on the main dashboard replaced with "Check for updates" — it was not inert, it was silently working.** The button called `POST /api/releases/deploy` (real, fully implemented, fleet-wide: pushes the cached latest release to every connected/approved/not-already-current device via `_run_update`, no confirmation step) and discarded the response on success with zero feedback — no log, no toast, no state change. Any prior click most likely deployed to the whole fleet silently. Server-side route is untouched and still fully functional, `@auth.require_admin`-gated as before; the dashboard's only call site for it is gone. Replaced with the same force-check pattern as the Updates tab — genuine live GitHub check, visible loading state, result feeds the existing "Latest Release" display. If fleet deploy is wanted back later, it should get a deliberate UI (e.g. a confirm step listing exactly which devices and versions) rather than a single bare button.
- v2.6.0 — 2026-07-02: ESPHome native API satellite integration — HA MVP. EchoMuse devices now speak the ESPHome native API on the controller's outward-facing side, making them HA-compatible voice satellites via HA's built-in ESPHome integration (no custom HACS component). Full wake-word → STT → intent → TTS → speaker round-trip confirmed working end-to-end on both physical devices against real HA Core 2026.6.4, alongside the existing `claracore` mode (`VOICE_MODE` env var gates the two, never concurrent).

  **New:** `em_esphome.py` + `esphome/` subpackage (`frame_protocol`, `message_registry`, `feature_flags`, `satellite_server`, vendored `api_pb2`/`api_options_pb2` from `aioesphomeapi==45.3.1`). DB migration v2 adds `esphome_api_port` and `esphome_noise_psk` (nullable placeholder, unused pending a future Noise-PSK follow-up) per-device columns. Ports allocated monotonically from 16001, never reused after deprovisioning (ESPHOME_SPEC.md §2.2) — a stale HA-side "add device" entry pointed at a freed port must never silently reattach to a different physical speaker.

  **Protocol findings confirmed against real HA Core 2026.6.4** (some corrected earlier assumptions from the design spec): `project_name` requires dot-notation (`EchoMuse.<label>`) — HA's `manager.py` splits on it unconditionally, IndexErrors silently and the device never appears in Devices & Services without it. Zero entities on `ListEntitiesResponse` → device silently ignored — `media_player` entity is mandatory. `ANNOUNCE` feature flag gates whether HA sends `VoiceAssistantConfigurationRequest` at all. `AuthenticationRequest` is sent by real HA regardless of `uses_password=False` — must be acknowledged, not ignored. `SubscribeVoiceAssistantRequest` **is** sent by real HA (an earlier design-phase assumption said it wasn't). `AnnounceFinished` must be yielded synchronously from `handle_message()` — the base dispatcher calls `list(handle_message(msg))`, so anything from an async task arrives too late for the wizard's own timeout. `MediaPlayerState` transitions `ANNOUNCING → AnnounceFinished → IDLE`, all three, are required for the setup wizard to pass. `ffmpeg` (apt-installed in the controller Dockerfile) decodes HA's MP3 TTS delivery to PCM. mDNS service names use `device_id[-12:]` rather than a prefix — both devices share a `G090LF11` prefix and would otherwise collide in HA's auto-discovery.

  **Bug: `VoiceAssistantEventType` import — wrong enum name, one-line fix.** The real enum (confirmed by installing `aioesphomeapi==45.3.1` fresh from PyPI and inspecting the generated `api_pb2.py` directly, not assumed from memory) is `VoiceAssistantEvent`, not `VoiceAssistantEventType`. Every `ET.VOICE_ASSISTANT_*` member reference downstream was already correct — only the import alias was wrong.

  **Non-bug: `VoiceAssistantResponse` "unhandled" log line was never a problem.** Cloned `OHF-Voice/linux-voice-assistant` (the official non-firmware reference satellite) directly and confirmed its `handle_message` dispatch has no branch for this message at all — it's HA's ack to the satellite's `VoiceAssistantRequest`, and its `port` field is only meaningful for the UDP-audio-return path real ESP32 firmware uses outside `API_AUDIO` mode; response audio here continues over the same TCP connection via `VoiceAssistantAudio` regardless. An explicit no-op branch with a comment was added anyway (cheap insurance against the next debugging session mistaking a `log.debug` no-op for a gap) — no behavioural change.

  **Bug: premature `RUN_END` silently ended turns before STT/TTS ever ran.** HA can — confirmed repeatedly on real hardware — emit a spurious `RUN_END` event moments after `VoiceAssistantRequest(start=True)`, before `STT_START` even arrives, distinct from the genuine terminal `RUN_END` that follows the real TTS sequence several seconds later. The old code treated *any* `RUN_END` as "nothing more is coming" and unblocked the turn-waiter immediately; that early unblock then won the race against `_stream_mic_audio` (still waiting on the device's VAD-end sentinel), so by the time the turn actually reached its TTS wait, the wait was already satisfied with no TTS URL — the turn ended silently, HA's STT/intent/TTS completed correctly in the background entirely unread, and HA's stock "Sorry, I couldn't understand that" response (the reply for a satellite that dropped off mid-pipeline, not a real transcription failure) is what actually played, if anything did. Fixed by tracking `INTENT_END` (the reliable "STT + intent resolution genuinely finished" marker, always after `STT_END`, always before `TTS_START`) and only letting `RUN_END` close the turn once that's been seen — a turn that legitimately ends with no spoken response still passes through `INTENT_END` first, so this doesn't introduce a stall for that case. Verified by replaying the exact logged event sequence from a real broken turn through the patched method and confirming it now survives the premature `RUN_END` and completes correctly on the real terminal sequence; separately confirmed the old logic reproduces the exact failure when fed the same sequence.

  **Bug: standalone announce (setup wizard audio test, push TTS) silently never reached the speaker.** `EchoMuseSatellite._on_announce` was a one-time value copy of `DeviceESPhomeServer._standalone_play`, taken at the moment HA's TCP connection was accepted (`_protocol_factory()`). `_standalone_play` itself is set by `device_connected()`, fired independently by the **physical Echo Dot's own** `/control` reconnect — no ordering guarantee exists between that and HA's independent ESPHome TCP connect, and in practice the HA-side connection routinely won the race, leaving the snapshot at `None` even on freshly-established connections (ruling out a simpler "just goes stale on reconnect" explanation — confirmed on multiple fresh connections in a row). Fixed by giving `EchoMuseSatellite` a back-reference to its owning `DeviceESPhomeServer` and reading `_standalone_play` live at call time in `_fetch_and_play_announce`, instead of ever snapshotting it. Verified by reproducing the exact ordering (satellite constructed before `_standalone_play` is set, callback wired afterward, announce arrives on that same already-constructed connection) and confirming audio now reaches the callback; separately confirmed the old snapshot logic reliably reproduces the exact "no playback callback set" log line under the same conditions.

  **New feature: local no-speech timeout, matching Alexa's "wake word, then silence" behaviour.** Previously, saying the wake word and then nothing left the listening ring lit for as long as HA's server-side VAD took to notice (observed: over two minutes of silence with no local bound at all — the satellite had no independent liveness guard for "no speech was ever detected," only the existing post-speech silence hysteresis and the unrelated 30s TTS-wait timeout, neither of which engages if speech never starts). `streamMic` (device/internal/client/data.go) now runs a 5s deadline from turn start, armed **only when `lock_mic: true`** — see the WebSocket Protocol section above for the wire-level detail and the regression this caused when first shipped without that gate (armed unconditionally, it silently killed the permanent wake-word listening stream 5s after every boot). Cancelled cleanly via `Timer.Stop()` (with the documented drain-on-race pattern) the instant real speech is first detected; from that point on, end-of-turn is owned entirely by the existing silence-after-speech hysteresis, unchanged. On the controller side, the resulting `0x05` sentinel sends an empty `VoiceAssistantAudio(end=True)` to close HA's already-open pipeline cleanly (a real, valid protocol message — confirmed against the actual `VoiceAssistantAudio` protobuf schema, not invented for this purpose) but skips the 30s TTS-response wait entirely, so a no-speech turn closes in ~5s rather than potentially 35s and without generating a spurious `stt-no-text-recognized` round-trip to HA. Device-side test coverage added (`internal/client/streamMic_test.go`, run against a reconstructed minimal module tree with stubbed `gorilla/websocket`/internal packages since this environment can't reach `proxy.golang.org`/`golang.org`): confirms idle listening survives extended silence indefinitely, a bounded voice turn correctly times out and sends the exact expected wire bytes, and mid-turn speech correctly cancels a genuinely-armed timer. Two of the three tests were initially written against the pre-gating design and passed for the wrong reason (no timer was ever armed in either) — caught and corrected once the `lock_mic` gate was added, since a test that can't fail isn't testing anything.

  **Cosmetic, not fixed:** every `VoiceAssistantEventResponse`/`VoiceAssistantResponse` still logs `unhandled message type ... (no response)` from the base dispatcher even when `EchoMuseSatellite.handle_message` genuinely handled it — a `return` (no `yield`) inside a generator function yields nothing, so `list(handle_message(msg))` comes back empty regardless of whether real work happened. Purely a misleading debug-log line, not a functional gap; left alone this session to avoid touching shared base-class logging behaviour in the same cycle as the real fixes above.
- v2.6.1 — 2026-07-03: TCP keepalive, bidirectional volume control, Assist satellite idle transition.

  **Bug: HA reconnect failure after HA restart.** After HA restarted (update-triggered or from the system menu), the ESPHome satellite showed as unavailable in HA and voice turns silently no-opped with `esphome: no active HA connection`. Rebooting the *physical Echo Dot* fixed it — the clue: `device_connected()`/`device_disconnected()` correctly manage the TCP listener socket lifecycle but there was no equivalent for the HA-side client connection. If HA died without a clean TCP FIN, asyncio never got `connection_lost()`, `_active_satellite` was never cleared, and `get_satellite()` returned a stale dead object forever. Fixed by enabling `SO_KEEPALIVE` with explicit Linux tuning in `PlaintextFrameProtocol.connection_made()` via `get_extra_info("socket")` — idle 30s, interval 10s, 3 probes, dead peer detected and `connection_lost()` fires within ~60s. Existing `_on_satellite_disconnected` → `_active_satellite = None` chain was already correct; it just never got invoked. Confirmed on real hardware against a graceful HA restart.

  **New feature: bidirectional volume control.** Volume is now shared state across HA's media player entity, the physical volume buttons, and ALSA — all three stay in sync, survive controller and device restarts, and never drift. Device side: `volumeController.Set()` fires `onVolumeChange` after every ALSA write; `SendVolumeState(level int)` pushes `{"type":"volume_state","level":N}` on change and on every connect; `OnVolumeSet(cb)` + `case "volume_set"` inbound dispatch; `SetVolume(level int)` exported on `Server`. Controller side: `Device.volume` (HA-normalised 0.0–1.0) seeded from `startupVolume` on connect; `volume_state` handler converts 0–175 int, updates `device.volume`, persists to config via read-modify-write; `update_device_volume()` in `em_esphome.py` updates `DeviceESPhomeServer.volume` and immediately pushes unsolicited `MediaPlayerStateResponse` to HA via `satellite._send_one()` (synchronous `transport.write`, no await); `_send_volume_set` async callable injected at `device_connected()` time (same pattern as `_standalone_play`); `MediaPlayerCommandRequest` handler now reads `msg.has_volume`/`msg.volume`, converts to 0–175 int, and fires `send_fn` via `asyncio.create_task`; all four `volume=1.0` literals replaced with `self._current_volume` property on `EchoMuseSatellite`. Boot-restore: the existing config push at `device_connected()` already carries `startupVolume` which `applyHardwareConfig()` applies to tinymix — now fed with a real persisted value rather than always the default.

  **Bug: Assist satellite panel stuck on "Responding" after voice turns.** HA's `RUN_END` arrives while the satellite is still fetching and playing TTS — HA considers its pipeline done, but the satellite hasn't signalled completion. Root cause confirmed by reading `OHF-Voice/linux-voice-assistant/satellite.py` directly: `_tts_finished()` sends `VoiceAssistantAnnounceFinished()` as the idle-transition signal — not a voice-protocol-specific message despite the name. Neither `VoiceAssistantRequest(start=False)` nor `MediaPlayerStateResponse(state=IDLE)` alone are sufficient (both were tried and failed before going to the source). Fixed by sending `VoiceAssistantAnnounceFinished(success=True)` followed by `MediaPlayerStateResponse(state=IDLE)` in `run_esphome_voice_turn`'s `finally` block, after TTS playback and buffer drain complete.
- v2.6.2 — 2026-07-03: Global fleet config with per-device overrides; change-password UI.

  **New feature: global device config with per-device override.** All device config (mic gain, VAD, beamforming, EQ, OWW model/threshold, startup volume) now has two layers: a fleet-wide global default stored in `system_config` (key `global_device_config`, JSON blob, same shape as `DEFAULT_DEVICE_CONFIG`) and an optional per-device override. DB migration v3 adds `use_global_config INTEGER NOT NULL DEFAULT 1` to `devices` — all existing devices default to inheriting fleet config with no behavioural change on upgrade.

  **Config resolution** is handled by `get_effective_device_config(device_id)` in `em_db.py`, which replaces the previous `get_device_config` call in `device_connected()`. When `use_global_config=1`, returns the global config; when `0`, returns the device's own config column. `set_device_use_global(device_id, enabled)` manages the flag — when reverting a device to global, it also resets the per-device config column to a copy of the current global so the stored value stays coherent if the flag is toggled again later.

  **startupVolume is always per-device.** Volume is hardware state set at provisioning, not fleet policy. `get_effective_device_config` always merges `startupVolume` from the per-device config column on top of whatever the global config returns, even when `use_global_config=1`. The `volume_state` read-modify-write path in `em_controller.py` already writes to the per-device column directly and is unchanged — the two mechanisms compose correctly: volume persists per-device, everything else inherits from global unless explicitly overridden.

  **API changes:** `GET /api/global/config` (any auth) serves fleet defaults. `POST /api/global/config` (admin) saves fleet defaults and immediately pushes the updated config to all currently-connected devices with `use_global_config=1`. `GET /api/devices/{id}/config` now returns `{config, use_global_config}` using effective config. `POST /api/devices/{id}/config` accepts an optional `use_global_config` bool in the body: `true` reverts to global (config fields in body ignored), `false` enables per-device override (supplied config written and pushed), absent leaves the flag unchanged (plain config update, used by the global push path). `_merge_device` now includes `use_global_config` so the dashboard has it without a separate fetch.

  **Dashboard — gear icon settings panel.** New `⚙` button in the header opens a `SettingsPanel` modal with two tabs. "Fleet Config" tab: `DeviceConfigForm` (new shared component, also used by the per-device config tab) pointing at global defaults — "Save & push to fleet" persists and pushes to all on-global devices immediately. "Account" tab: change-password form (current password, new password, confirm) backed by new `POST /api/auth/change-password` endpoint — any authenticated user, verifies current password via bcrypt before accepting the new hash. Does not invalidate existing sessions.

  **Dashboard — per-device config tab redesigned.** Toggle banner at the top shows current state: blue tint ("Using fleet config") or green tint ("Device-specific config"). Enabling the toggle seeds local config state from the current global config and makes all controls editable; push sends `{use_global_config: false, ...config}`. Disabling reverts to fleet defaults — push sends `{use_global_config: true}`, body otherwise ignored. Controls render at 45% opacity with `pointer-events: none` when on global — values visible but not interactive without explicitly enabling the override.

*Device: Echo Dot 2nd Gen (RS03QR). Tested on macOS with ADB 35.0.2.*
