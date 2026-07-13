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
✅ NS and AGC independently toggleable from dashboard — NS off (pending P0-3); AGC applies to lock_mic turns only since v2.7.0 (wake stream is permanently AGC-free)
✅ Ungated continuous wake stream (v2.7.0) — no VAD gate/AGC/preroll on the always-on stream; OWW scores uninterrupted audio; ~32KB/s per device
✅ Mic stream leak fixed (v2.7.0) — ownership check in streamMic exit; stop/start pairs can no longer leak a concurrent duplicate stream (historical "wake degrades over days, reboot fixes it" root cause)
✅ Per-room noise floor tracking (v2.7.0, controller) — measurement-only asymmetric EWMA; drives the SNR-relative 5s no-speech cutoff (wake-then-silence closes quietly again)
✅ Mid-stream beam lock (v2.7.0) — beam_lock/beam_unlock control messages; wake turns get perimeter mic selection without a stream restart
✅ Beamformer lock-back selection (v2.7.2) — Lock() scores directions over a ~2s energy-history ring covering the wake word, not the decayed present (see pipeline state table)
✅ Acoustic echo cancellation (v2.7.3, working since v2.7.7, convergence holds since v2.7.8, default OFF) — speexdsp canceller on the whole mic path; reference tapped at the speaker ALSA write. Keep aecDelayMs at 0 (measured; higher values are non-causal — see v2.7.7). Converges to ~14dB per response and *stays* converged across turns since v2.7.8 (governor trims no longer reset the filter); `[aec] att=` and `[mic] clock/stall` telemetry in the device log show live attenuation and capture health. Enable from the dashboard Microphones advanced section
✅ 24-bit fixed mic gain (v2.7.1) — `micGainDb` (default +24dB) applied to the full 24-bit sample during S16 extraction; recovers the low byte the old truncation discarded (speech was ~3–20 LSB in 16-bit). Validated: STT empty-transcript rate went from 6/19 turns to 0/5, detection rms 0.0003 → 0.006–0.009, clipped=0
✅ PTY dashboard shell (v2.7.1) — device allocates a real pseudo-terminal (mksh prompt, line editing, top/vi, resize); dashboard terminal is xterm.js; programmatic sessions (OTA) keep the raw pipe
✅ /tmp/server.log size cap (v2.7.1) — trim loop in start_server.sh, bounded at ~5.5MB; VAD diag slowed to ~10min with prompt clip-count reporting
✅ State-aware landing page (v2.7.1) — / shows first-run setup (amber ring) or login (green ring) and redirects authenticated visitors to /dashboard; sessions in localStorage
✅ HA-driven conversation continuation — continue_conversation flag wired; after TTS playback, re-triggers voice turn immediately if HA sets flag in INTENT_END (v2.6.4)
✅ Speaker audioChanDepth 32 — prevents mid-stream underrun stutter on longer TTS responses (v2.6.4)
✅ Dashboard offline IP display — shows last known IP with "(last seen)" annotation when offline; suppresses Docker-NAT 127.0.0.1 artefact (v2.6.4)
✅ Per-turn structured trace — [TURN] log line with full stage timing at turn end
✅ OWW near-miss visibility — scores > 0.05 logged at INFO (rate-limited 1/2s per device), persistent counter on dashboard status tab (v2.6.5)
✅ VAD threshold tunable down to 0.0001 (dashboard slider floor corrected)
✅ Beamformer structural fix — smoothers always run, output by lock state not flag
✅ AGC release frozen during silence — prevents noise floor amplification past VAD threshold
✅ Acoustic feedback fix — controller sleeps for audio duration after EOS before mic restart
✅ Spinner runs for full response duration — duration calculated from PCM length
✅ VAD threshold default 0.001 — matches measured conversational speech range at 1.3m (v2.6.5; was 0.003, which sat above soft speech)
✅ Mute button — toggles mic mute, red LED ring, blocks action button
✅ Volume buttons — local interception, cyan LED ring feedback
✅ Amp boot click suppressed — mute → clock DAC with silence → amp on → unmute ordering in pcm_speaker.go Init (fixed order 2026-07-10)
✅ Amp idle hiss eliminated — graceful SIGTERM shutdown mutes + disables amp (PcmSpeaker.Close); start_server.sh repeats amp-off after every server exit as SIGKILL/panic backstop
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
✅ Safe per-device WiFi change (dashboard WiFi tab) — device-side executor with auto-rollback: full wpa_supplicant.conf replacement written *while WiFi is disabled* + verified `svc wifi` bounce (via sh — the script has no shebang), gated on associate-to-target-SSID ≤45s → IP ≤20s → controller reconnect ≤90s; any failure restores the backed-up config; uncommitted changes roll back on boot (pending-marker recovery, same philosophy as the A/B binary slots); result delivery is at-least-once (re-sent until the controller's wifi_commit ack); last-known-controller-address fast path makes cross-subnet controllers reachable without mDNS. All three paths hardware-validated 2026-07-11: rollback (garbage SSID, 65s round trip), startup recovery, happy path (30s)
✅ LED ring scenes (controller-rendered) — Standard/Airy/Malevolent/Pride/Custom palettes for the listening ring and thinking spinner (em_scenes.py); mute ring stays red and volume arc stays cyan in every scene; frames carry an explicit `listening` flag so the device's direction overlay works on any colour (falls back to the all-green heuristic for old controllers), and the overlay brightens the scene colour instead of painting green
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
✅ HA VAD-end is the turn endpointing authority — _stream_mic_audio exits on HA's STT_VAD_END/ERROR, device RMS-gate sentinel advisory, 20s hard cap; fixes stuck spinner in noisy rooms (v2.6.5, C1)
✅ Conversation continuation actually works — mic restarted before each continuation turn; shipped broken in v2.6.4 (v2.6.5, C2)
✅ Preroll discard wake-turns-only — button/continuation turns pass 0, no first-word clipping on those paths (v2.6.5, C3)
✅ Mute is device-authoritative — mute stops the running mic stream, unmute restores it; audio stops leaving the device while the ring is red (v2.6.5, C5 partial — full-chip ADC mute pending)
✅ OWW speex NS toggle (owwSpeexNs) — openwakeword's 16kHz-native speexdsp suppressor on the wake path only, dashboard/API/DB wired, off by default (v2.6.5, Q1)
✅ Device preroll ring — ~512ms of pre-gate audio flushed on VAD gate open; fixes onset splice that depressed OWW scores and clipped first phonemes (v2.6.5)
✅ AGC reset at every mic stream start + mic stopped before TTS playback — TTS-echo-crushed gain can't poison the next turn; enabled AGC re-enable (v2.6.5)
✅ Speaker EOS vs underrun disambiguation — 0x03 EOS sets EndStream(), natural drain no longer logged as underrun (v2.6.5)
✅ Mic queue overflow drops oldest frame, not newest — audio tail stays contiguous with real time (v2.6.5)
✅ voice_queue drained before oww_paused routing flip — stale ambient frames no longer bleed into the next turn as STT preamble (v2.6.5 regression fix)
✅ ADC mute controls identified for all four chips — tinymix dump in device/tools/ confirms B–D at 123/124, 141/142, 159/160
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

## Mic Array — What Actually Happens at Each Stage

This describes the pipeline as of v2.7.1 (2026-07-07): the wake stream is **ungated and AGC-free** — the device streams every period continuously, and all adaptation lives controller-side as measurement. The only gain in the path is the fixed 24-bit mic gain (v2.7.1). NS stays off pending P0-3. The stages are in order from hardware to HA.

Why the gate came out (2026-07-06 rework): the VAD gate's absolute RMS threshold is wrong in at least one room of every home, openwakeword is a streaming model that scores best on continuous audio (gated bursts spliced together measurably depress scores even with preroll), and the AGC's persistent gain state on a never-restarting stream rebaselined itself to each room's noise floor — the "wake word degrades over days, reboot fixes it" disease. Bandwidth was the reason for the gate and it doesn't survive arithmetic: 16kHz mono S16 is 32KB/s per device, 6× smaller than the TTS playback stream.

### Idle — waiting for wake word

```
ALSA card 0 device 24 (9ch S24_3LE 16kHz)
  → pcm_microphone.go subscriber channel (raw 13824-byte periods at ~31ms intervals)
  → beamformer.Process(raw, beamAngle, gain)
      — unlocked (idle): always returns ch6 (centre/omni mic)
      — smoothers still update every period (baseline stays warm;
        energy ratios are gain-invariant)
      — fixed mic gain (micGainDb, default +24dB) applied to the FULL
        24-bit sample during S16 extraction (v2.7.1) — the old path took
        the upper 2 bytes and threw away the low byte, where nearly all
        of the signal lives at this hardware's capture levels (speech
        ≈ −70dBFS raw). Clipped samples are counted and reported.
      — returns mono S16_LE 512 samples
  → vadPeriodRMS(mono) — computed for the periodic diagnostic log only
      (every ~10min, or within ~16s of a clipped sample — v2.7.1);
      does NOT gate sending on this stream (v2.7.0)
  → [if NS enabled] proc.noiseSuppress() — RNNoise (currently OFF, pending P0-3)
  → AGC: NEVER on the wake stream (v2.7.0 — forced off regardless of config;
      adaptive gain state on a permanent stream is a rebaselining mechanism
      by construction). agcEnabled config now applies to lock_mic turn
      streams only.
  → EVERY period sent — batched into 80ms chunks, ~12.5 frames/s, 32KB/s:
      frame: [0x01][seq_hi][seq_lo][2560 bytes PCM = 80ms]
      No VAD gate, no preroll ring, no 0x04/0x05 sentinels on this stream.

Controller handle_data():
  → oww_paused.is_set()? → voice_queue (during a turn)
  → else → mic_queue (during idle)

wake_word_listener():
  → pulls from mic_queue (10s of silence now means the stream DIED —
    hardware mute still produces zero-filled frames — so the controller
    logs a warning and sends a defensive mic_start, skipped mid-turn)
  → accumulates into 80ms chunks
  → per-chunk RMS updates device.noise_floor (v2.7.0): asymmetric EWMA,
    follows drops fast (α=0.3), rises slowly (α=0.008 ≈ 10s) so speech
    can't drag it up. Measurement only — the audio is never modified.
  → OWW inference (hey_rhasspy_v0.1, threshold 0.30)
  → scores > 0.05 counted as near-misses: INFO log (rate-limited 1/2s,
    now includes rms= and floor=) + dashboard counter
  → score >= threshold → wake detected
```

**Key: the stream runs continuously and is completely stateless — no gate, no adaptive gain, nothing that can drift with room history. OWW always sees uninterrupted audio. ch6 omni during idle. Per-room adaptation happens controller-side as a noise-floor *measurement*, consumed by endpointing — never applied to the signal.**

### Wake word detected → command capture

```
wake_word_listener():
  → oww_paused.set() — routing flips: handle_data() now sends to voice_queue
  → model.reset(), buf.clear()
  → beam_lock control message (v2.7.0) — device locks the beamformer onto
    the perimeter mic with the best speech onset ratio, mid-stream, no
    restart. Sent at detection because that's the freshest onset signal the
    selector will get (though see the beamforming caveat in the table below
    — controller-side detection latency means even this is 300–500ms after
    the wake word started). beam_unlock is sent after the turn completes.
  → _run_voice_locked(device, trigger_label="wakeword(score)")
      → [esphome path] trigger_voice_turn()
          → TurnTrace created (t0 = now)
          → satellite.run_esphome_voice_turn()
              → VoiceAssistantRequest(start=True) → HA Assist pipeline opens
              → _stream_mic_audio() starts reading from voice_queue
                (whole phase wrapped in a 20s hard cap, v2.6.5 C1):
                  → first 3 frames discarded (VOICE_PREROLL_DISCARD=3, 240ms)
                    — removes wake-word tail ("...Jarvis") from audio.
                    WAKE TURNS ONLY (v2.6.5 C3): button and continuation
                    turns pass preroll_discard=0 — they have no wake-word
                    tail, discarding real audio clipped their first word
                  → controller-side 5s no-speech timeout armed
                  → timeout disarms on SPEECH, not on the first frame
                    (v2.7.0 — frames now flow continuously, silence included,
                    so "a frame arrived" means nothing). Speech = chunk RMS ≥
                    max(3 × device.noise_floor, 0.004), OR HA's own
                    STT_VAD_START event (covers quiet speech in a noisy room).
                    Wake-then-silence closes quietly at 5s again instead of
                    sitting through HA's ~10s STT timeout + error cleanup.
                  → frames sent as VoiceAssistantAudio chunks to HA
                  → stream ends on WHICHEVER ARRIVES FIRST:
                      — HA's own VAD end (_ha_vad_end, set on STT_VAD_END or
                        ERROR) — the endpointing authority; noise-robust,
                        model-driven (v2.6.5 C1)
                      — device VAD sentinel (0x04) — only exists on lock_mic
                        (button) streams now; never arrives on wake turns
                    → VoiceAssistantAudio(end=True), t_vad_end logged

NOTE: the stream never stops. No mic_stop, no mic_start_turn on OWW path.
The only changes at wake are the oww_paused flag flipping the queue routing
and the beam_lock switching the mic channel. Command audio flows in with
zero gap.
```

### HA pipeline → response

```
HA Assist:
  → STT (Whisper) → intent resolution → TTS generation
  → VoiceAssistantEvent stream: RUN_START → STT_START → STT_END →
    INTENT_END → TTS_START → TTS_END → RUN_END

Controller satellite:
  → INTENT_END received → tts_event armed (prevents premature RUN_END close)
  → TTS_URL received → t_tts_url_ms logged
  → fetch MP3 from HA TTS proxy → ffmpeg decode → 22050Hz mono S16_LE PCM
  → t_tts_fetched_ms, tts_bytes logged
  → EQ + resample 22050→48000Hz stereo
  → mic_stop → device stream stops BEFORE playback starts (v2.6.5 —
    previously only in the post-turn finally, so the device processed
    63–65 frames of its own TTS echo per turn, contended the Wi-Fi radio
    against the incoming speaker frames, and crushed AGC gain)
  → stream PCM to device ALSA as 0x02 binary frames, 0x03 EOS
  → sleep for audio duration (acoustic feedback prevention)
  → EITHER (continuation, v2.6.5 C2): HA set continue_conversation →
    mic_start (no lock_mic) → loop into next turn with preroll_discard=0.
    The restarted stream is ungated so audio flows immediately; the
    controller sends beam_lock again the moment the user's answer clears
    the noise floor (the TTS mic restart reset the beam to ch6 omni)
  → OR (normal end): voice_queue drained WHILE oww_paused is still set
    (v2.6.5 regression fix — draining after the routing flip left stale
    ambient frames to arrive as preamble on the next turn)
  → oww_paused.clear() → routing returns to mic_queue
  → mic_start (no lock_mic) → stream restarts on ch6 omni
  → beam_unlock sent (belt-and-braces — matters for no-TTS turns where the
    stream never restarted and a beam lock would otherwise persist into
    idle wake listening)
  → stale frames drained (belt-and-braces no-op now), OWW model reset
  → [TURN] log line emitted with full timing breakdown
```

NOTE the stop/start pair around TTS is safe as of v2.7.0: streamMic's exit
path has an ownership check (d.micStopCh == stopCh) so a draining old
goroutine can't clear micActive over its replacement. Before the fix, that
race let a mic_start spawn a second concurrent stream that no mic_stop could
reach — leaked gated streams were silent while idle but duplicated every
utterance 2× (STT saw "turn on on the on the office…") and their 0x04
sentinels cleared the OWW buffer, progressively killing wake detection until
the process restarted. This was almost certainly the historical
"wake word degrades over days, reboot fixes it" bug.

### Button-triggered turn (differs from OWW path)

```
Button press (clickType=138):
  → oww_paused.set()
  → mic_stop → device stream stops
  → mic_start(lock_mic:true) → new stream with lockMic=true
      → beam.Lock(beamformingEnabled) called
        — beamformingEnabled=true: selects perimeter mic with highest onset ratio
        — beamformingEnabled=false: Lock() no-ops, stays on ch6
      → [beam] locked to chX (Y°) onset_ratio=Z logged
  → _run_voice_locked(device, trigger_label="button")
  → [same HA pipeline as above]
  → mic_stop → mic_start (no lock_mic) → back to ch6 omni
    (explicit stop first, v2.7.0: on no-TTS outcomes — cancel, error,
    no-speech — the lock_mic stream is still running and a bare mic_start
    would no-op against it, leaving the GATED, beam-locked turn stream as
    the permanent wake stream)

Button path retains stop/start because: (a) no dead zone cost — button is
pressed before speech starts, (b) the lock_mic stream is the only place the
VAD gate, preroll ring, sentinels, and (config-gated) AGC still exist.
```

### What's currently off and why

| Stage | State | Reason |
|---|---|---|
| RNNoise NS | OFF | Model calibrated for 48kHz, fed 16kHz — miscalibrates speech probability, degrades HF consonants. Measured improvement when disabled. Decision pending (P0-3): speexdsp preprocessor (16kHz-native) or delete — with the wake stream now ungated, the cleaner future is NS controller-side on ASR-bound utterances only. |
| AGC | **OFF on the wake stream, permanently** (v2.7.0 — ignores config). Config-gated on lock_mic turns only. | v2.6.5 re-enabled it after the echo fixes, but ResetAGC only runs at stream start and the wake stream never restarts — in any room with steady noise above vadThreshold, the release path walked gain up toward amplifying the noise floor (the RNNoise interlock that was meant to prevent this is dead while NS is off), then the fast attack compressed the wake word's envelope mid-utterance. Adaptive gain state on a permanent stream = rebaselining by construction. **Open item:** the fixed gain staging that should replace it — speech at 1.3m measures RMS ~0.001 vs the 0.08 the AGC used to target; wake scores dropped noticeably without it. Use the rms=/floor= values now in the OWW logs to size an adcDigitalGain/adcMicpga bump. |
| VAD gate (wake stream) | **REMOVED** (v2.7.0) | Absolute RMS threshold can't be right in every room; OWW wants continuous audio; the gate held open by ambient noise was also what let the AGC release run continuously. Still exists on lock_mic (button) streams for endpointing. |
| Beamforming | ON in config, **lock-back selection (v2.7.2)** | Lock is commanded at wake detection (v2.7.0, beam_lock mid-stream); detection lands 300–500ms after the wake word ends, so live onset ratios had decayed and selection was known-poor. Fixed via lock-back: a ~2s ring of per-direction period energies (frozen while locked, like the baseline); Lock() scores each direction by its top-8-period burst within the window relative to its baseline, so it selects on the recorded wake word rather than the decayed present. Unit-tested (TV-vs-decayed-speaker scenario in `beamformer_test.go`). Known caveat: TTS echo enters the ring between turns — the baseline absorbs the same energy, damping its ratio, but continuation-turn locks are the weaker case until AEC. Validate direction LED against speaker position after OTA. |
| owwSpeexNs | OFF | Available (v2.6.5, Q1): openwakeword's speexdsp suppressor, wake path only. Off by default — flip on the lounge device and A/B wake rate with TV on before fleet-wide enable. |
| Noise floor tracking | **ON** (v2.7.0, controller) | Per-device asymmetric EWMA over the continuous wake stream. Measurement only. Consumed by the SNR-relative no-speech timeout; logged as floor= in OWW lines. |

### VAD threshold guidance

**Units (v2.7.1):** all values below are *pre-gain* — measured before the fixed `micGainDb` stage. The device scales `vadThreshold` by the linear gain internally, so the config value keeps these units regardless of the gain setting; the `rms=`/`floor=` values in controller logs are *post-gain* (multiply this table by ~16 at the default +24dB to compare).

Measured signal levels at 16kHz on ch6, MICPGA=40, digital gain=88:

| Condition | Typical RMS |
|---|---|
| Dead silence (quiet room) | 0.00017–0.00019 |
| Ambient room noise | 0.00020–0.00050 |
| Conversational speech at 1.3m | 0.0004–0.0010 |
| Raised voice at 1.3m | 0.004–0.010 |

vadThreshold 0.001 sits comfortably between ambient and speech. Raise to 0.003–0.005 in noisy rooms (TV on). Dashboard slider now goes down to 0.0001 for quiet environments.

**Scope change (v2.7.0):** vadThreshold/vadSpeechMs/vadSilenceMs apply only to lock_mic (button) turn streams now — the wake stream is ungated and ignores all three. Wake-turn endpointing is HA's VAD; accidental-wake cutoff is the controller's 5s SNR-relative timeout against the measured per-room noise floor (no per-room tuning needed). The fixed-gain bump this table originally motivated shipped in v2.7.1 (`micGainDb`).

---

## Voice Pipeline

```
"Hey Jarvis"
    → on-device energy VAD (RMS ≥ 0.001, normalised, pre-AGC)
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
      (wake turns drop the first 240ms of wake-word tail; button and
      continuation turns don't — v2.6.5 C3)
    → VAD end → VoiceAssistantAudio(end=True)
      — HA's STT_VAD_END is the endpointing authority (v2.6.5 C1); the
        device's own RMS-gate 0x04 sentinel is advisory and ends the
        stream only if it arrives first. 20s hard cap as backstop.
    → HA: STT (Whisper) → intent → TTS
    → HA: VoiceAssistantAnnounceRequest(media_id=url, text="...")
    → controller: mic_stop (acoustic-feedback guard, v2.6.5)
    → controller: fetch MP3 (one retry on transient failure, ffmpeg decode
      capped at 15s) → 22050Hz mono S16_LE PCM
    → controller: EQ + resample 22050→48000Hz stereo → stream to device ALSA
    → controller: MediaPlayerState ANNOUNCING → AnnounceFinished → IDLE
    → if HA set continue_conversation: mic_start → next turn immediately
      (preroll_discard=0), no wake word needed (v2.6.5 C2)
    → else: LED off, voice_queue drained, mic restart
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
{"type": "config", "adcDigitalGain": 88, "adcMicpga": 40, "vadThreshold": 0.001, ...}
{"type": "leds", "leds": [{"id": 0, "r": 0, "g": 180, "b": 0}, ...]}
{"type": "mic_start"}
{"type": "mic_start", "lock_mic": true}
{"type": "mic_stop"}
{"type": "beam_lock"}      // v2.7.0: lock beamformer onto best perimeter mic
                           // mid-stream, no restart (no-op if beamforming
                           // disabled in config or already locked)
{"type": "beam_unlock"}    // v2.7.0: release beam lock, back to ch6 omni
{"type": "shell_open"}
{"type": "shell_close"}
{"type": "ping"}
```

### Data plane (`ws://server:8767/data`) — binary

Device → Server (mic frames):
```
[0x01][seq_hi][seq_lo][mono S16_LE PCM, 2560 bytes = 80ms]  — audio (continuous on the wake stream since v2.7.0; VAD-gated speech on lock_mic streams)
[0x01][seq_hi][seq_lo][0x04]                                 — VAD end (lock_mic streams only since v2.7.0)
[0x01][seq_hi][seq_lo][0x05]                                 — no-speech timeout (lock_mic streams only; see below)
```
All three share the same `frameTypeMic` (`0x01`) wrapper and seq header — the VAD sentinels are single-byte *payloads*, not distinct top-level frame types. (0x02/0x03 below are genuinely distinct top-level types, speaker-direction only, no seq header — don't confuse the two framing conventions.)

**No-speech timeout (0x05), added v2.6.0.** `streamMic` (device/internal/client/data.go) only arms this when `lock_mic: true` was set on the `mic_start` that began the stream — i.e. only for a bounded voice turn (post-wake-word or button press), never for the permanent `lock_mic`-absent OWW listening stream. If no speech is ever detected (RMS never crosses `VadThreshold` for `VadSpeechMs` consecutive periods) within 5s of turn start, the device gives up locally and sends `0x05` instead of waiting on the existing silence-after-speech hysteresis, which never engages if speech never started. Distinguishing 0x05 from 0x04 lets the controller skip contacting HA's Assist pipeline entirely for a turn that never had anything to transcribe — mirrors Alexa's behaviour of quietly giving up on "wake word, then silence" rather than round-tripping to the backend just to receive `stt-no-text-recognized`. **This must never be armed for `lock_mic`-absent streams** — an earlier build armed it unconditionally, which silently killed the permanent wake-word listening stream 5s after every boot/reconnect with nothing to restart it (wake word "stopped working entirely," diagnosed via device log showing repeated `no speech detected within timeout` firing exactly 5s after every idle `Mic streaming started`, with no corresponding `mic_start` to revive it). Since v2.7.0 the failure mode is doubly covered: the `lock_mic`-absent stream has no VAD machinery at all, and the controller detects a dead wake stream (10s without frames) and sends a defensive `mic_start`.

Server → Device (speaker frames):
```
[0x02][stereo S16_LE PCM, 8192 bytes = one ALSA period]
[0x03] end of stream
```

### Shell plane (`ws://server:8767/shell/{device_id}`) — binary

Demand-opened by the Go binary dialling **outbound** to the controller on receipt of a `shell_open` control message. Single session enforced. The controller proxies this connection to the dashboard terminal. No inbound ports on the device.

Two modes (v2.7.1):

- **PTY** (`shell_open` with `pty: true` — dashboard sessions): the device attaches `/system/bin/sh` to a real pseudo-terminal (`/dev/ptmx`, `TERM=xterm-256color`, new session with controlling TTY), giving an interactive mksh with prompt, line editing, job control, and full-screen apps. The device signals the established mode by dialling `/shell/{device_id}?pty=1`; the controller relays it to the dashboard as a `shell_meta` text message before any bytes flow. Input from the dashboard is framed binary: `0x00` + stdin bytes, or `0x01` + cols/rows (uint16 BE each) for resize (`TIOCSWINSZ`). Output is raw. If PTY allocation fails, the device falls back to the pipe and omits the query flag.
- **Pipe** (`pty` absent — programmatic sessions: OTA transfer, `_shell_run`): raw unframed stdin/stdout, no echo, no prompt — exactly what the output-parsing callers need. Unchanged from earlier versions.

The controller proxies bytes verbatim in both modes; the framing is interpreted only at the endpoints.

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

Wake word detection uses ch6 (centre/omni). VAD threshold defaults to 0.001 normalised RMS — adjustable via config push from the dashboard. In noisy environments, raise to 0.003–0.005.

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

**Amp click/hiss suppression.** Order matters (found on hardware, 2026-07-10): `pcm_speaker.go` Init() mutes the output (tinymix ctl 61 → 0), opens the PCM stream and lets the silence loop clock the DAC for ~100ms, *then* enables the amp (ctl 5 On), waits 50ms for it to settle, and unmutes last. Enabling the amp onto a floating (unclocked) DAC and unmuting before stream-open was the source of the click on every service start. Shutdown is the mirror image: on SIGTERM the server's `PcmSpeaker.Close()` mutes → amp off → closes the stream, and `start_server.sh` repeats mute + amp-off after every server exit (covering SIGKILL/panic) — an enabled amp on an idle DAC audibly hisses for as long as the server is down (worst case: between OTA slots).

**Mute implementation.** The mute button (KEY_MUTE, evdev code 113) arrives on `/dev/input/event1`. Mute is implemented by setting ADC_A Left/Right Mute (tinymix ctls 105 and 106). The mute controller intercepts the button locally, applies the tinymix change, updates the LED ring (red = muted), and signals the server to block dot button events. As of v2.6.5, mute also stops a running mic stream (and unmute restores the OWW listening stream) — audio stops leaving the device over the network regardless of controller state, not just future `mic_start` calls being refused.

**Mute hardware coverage — known gap.** Ctls 105/106 mute chip A only (ch0/ch1). Chips B–D — including ch6, the mic OWW and STT actually use — stay physically hot; the stream-stop above is what makes mute effective today. The full `tinymix -D 0` dump (`device/tools/tinymix_controls_output.txt`, captured 2026-07-06) confirms the sibling mute controls: ADC_B 123/124, ADC_C 141/142, ADC_D 159/160. Adding all four pairs to `applyMute`/`applyUnmute` in `internal/server/mute.go` is the next device-rebuild item.

**Mic gain — all four ADCs.** All four ADC pairs (A–D) are set to digital volume 88 and MICPGA 40. This matches Amazon's own initialisation values confirmed by analysing the unmodified device mixer state. Equalising all four ensures consistent sensitivity across all perimeter mics for directional selection.

**WiFi wake lock.** FireOS aggressively suspends the WiFi interface during inactivity, dropping WebSocket connections. Writing `"EchoMuse"` to `/sys/power/wake_lock` prevents this.

**Speaker streaming.** Audio is streamed as binary frames (8192 bytes = one ALSA period) over the data plane WebSocket. The device maintains a priority channel — the silence loop yields to real audio naturally, with backpressure at ALSA playback rate (~42ms/period). Piper TTS output (22050Hz mono) is resampled server-side to 48000Hz stereo before streaming.

**OWW threshold.** 0.3 works well for a London/Bristol accent — the default 0.5 is calibrated for American English.

**VAD threshold.** 0.001 normalised RMS is the default (v2.6.5 — corrected from a drifted 0.003 that sat above measured conversational speech at 1.3m). Adjustable via config push from the dashboard — no rebuild required. In noisy environments (music, TV), raise to 0.003–0.005.

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
- **PTY shell** ✅ — complete as of v2.7.1. Hand-rolled `/dev/ptmx` + `x/sys/unix` ioctls (no creack/pty dependency) + xterm.js in dashboard. Note: FireOS toolbox `top` is a dumb scroller by design — install a static busybox on `/data` for a redrawing `top`, `vi`, `less`, etc.
- **Acoustic echo cancellation** ✅ — shipped v2.7.3, functional as of v2.7.7 (silent buffer-size bypass), holds convergence as of v2.7.8 (mic capture overruns tripped the reference governor every ~20s and its filter reset threw away convergence — see the v2.7.8 changelog). speexdsp, ~14dB per response measured on hardware. Future: per-beam-channel filter states so a new-channel turn starts converged; the hardware echo-reference experiment (`Audio_ExtCodec_EchoRef_Switch` + the two unaccounted capture channels — a sample-synchronous reference would delete the ring/governor entirely); possibly speex residual echo suppression on the barge watcher path.
- **Media player integration** — pause room audio on wake word, resume after response (Home Assistant `media_player` service call)
- **Bermuda BT proxy** — room-level presence detection via Bluetooth, once fleet of 5–6 Echo Dots is deployed
- **Adaptive VAD** — calibrate threshold on startup from ambient noise floor × multiplier. Currently fixed at 0.001; in very noisy environments this may need runtime adjustment.
- **RNNoise model upgrade** — vendored v0.1 model (2018). Newer models available via binary blob download; requires model loading API (rnnoise_model_from_file) present in newer source but needing the xiph.org CDN which was unavailable. v0.1 performs well for home environment use.
- **Startup chime** — short audio signature on EchoMuse init
- **Holding response** — play audio while Clara is thinking if response takes >2s
- **ESPHome native API satellite integration** ✅ — complete as of v2.6.0. Both devices registered in HA, voice turns working end-to-end.
- **Bidirectional volume control** ✅ — complete as of v2.6.1. Physical buttons, HA media player slider, and ALSA mixer all stay in sync. Survives controller and device restarts.
- **Continue-conversation without re-waking** ✅ — complete as of v2.6.4. HA-driven: `continue_conversation` flag in `INTENT_END` re-triggers a voice turn immediately after TTS playback without requiring the wake word. User-driven follow-up window (always-on N-second listen after any response) is the next step — recommend as a dashboard toggle, default off, to avoid false triggers in noisy rooms.
- **Rubbish transcription suppression** — wake word triggers on background noise still result in HA's "Sorry, I couldn't understand that" response. Options: audio energy gate in `_stream_mic_audio` before sending to HA (cleanest); or discard HA's stock apology in the TTS handler (tactical). Deferred pending P0-3 NS fix — noise floor situation needs to settle first.
- **C5 hardware half — full-chip ADC mute** ✅ — complete as of v2.7.4. All four codec mute pairs (105/106, 123/124, 141/142, 159/160) toggled by `applyMute`/`applyUnmute`; the red mute ring now physically mutes every chip, including ch6.
- **Q5 — remove speaker underrun instrumentation** ✅ — complete as of v2.7.4. Underrun WARNING removed after several clean sessions; the dead legacy `Pump()` method (HTTP speaker path) removed from the Speaker interface with it.
- **§3.2 Wake-word barge-in** ✅ — complete as of v2.7.6 (`bargeInEnabled`, default off; requires device AEC). With it on, the mic streams through TTS playback and a dedicated per-device OWW watcher scores voice_queue at bargeInThreshold (used as-is since v2.7.7 — deliberately *below* owwThreshold, ~0.10: speech-over-TTS scores are depressed ~25dB by the speaker's loudness while post-AEC self-echo scores only ~0.004); detection sets cancel_event (aborts streaming + drain sleep), sends the new `speaker_flush` control message (device discards buffered periods; ≤~170ms ALSA in-flight still plays), and the turn loop re-enters a fresh turn with the wake-word preroll discard. Since v2.7.8 the filter stays converged across turns, so self-echo peaks at 0.002–0.003 and the threshold can sit at the 0.05 slider floor for easier interrupting. VAD-based barge-in (interrupt by just talking) remains explicitly out of scope.
- **§3.3 NS decision (P0-3)** — RNNoise is 48kHz-calibrated but fed 16kHz. Cheapest path: leave device NS off and test owwSpeexNs on the wake path. If device NS proves needed: replace RNNoise with speexdsp's 16kHz-native preprocessor, then delete the vendored RNNoise.
- **Device preroll ring (§3.4)** ✅ — complete as of v2.6.5. ~512ms of pre-gate audio flushed on VAD gate open; benefits wake onsets and continuation turns (gate starts closed after mic restart).
- **§3.5 Beamformer buffer reuse** ✅ — complete as of v2.7.4. Analysis buffers allocated once in `New()`, reused per period; `extractChannel` still allocates (data.go's preroll ring retains its slices).
- **§3.6 VAD sentinel encoding (B5)** ✅ — complete as of v2.7.4. Sentinel type travels in the queue item (`VAD_SENTINEL_END`/`VAD_SENTINEL_TIMEOUT` strings, defined in em_esphome); `last_vad_was_timeout` deleted.

---

**Document version:** v2.7.8
**Last updated:** 2026-07-08
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

- v2.6.3 — 2026-07-04: Speech quality overhaul — dead zone removal, pipeline diagnostics, beamformer structural fix, pipeline toggles.

  This session was driven by a formal speech quality review (SPEECH_QUALITY_REVIEW_Findings_-_4-7-26.md) identifying multiple architectural issues in the mic→OWW→STT chain. The primary symptom was needing to pause after the wake word and over-enunciate the command — same hardware had worked fine under Alexa. Changes are ordered by actual impact as discovered through instrumentation.

  **Root cause analysis — what the instrumentation revealed.** Before fixing anything, VAD diagnostic logging was added to `streamMic` (periodic RMS every ~3.2s) and OWW near-miss score logging added to `wake_word_listener` (scores above 0.05 logged at DEBUG). The data showed: idle RMS at 1.3m is 0.00017–0.00019; conversational speech hits 0.0004–0.0009; vadThreshold was 0.003–0.004 — a 6–10× gap. The device VAD gate was barely opening at conversational distance, so OWW was receiving near-silence. Additionally, AGC was drifting to near its 20× maximum during idle periods, amplifying room noise above VAD threshold and poisoning OWW's internal state with noise frames. RNNoise, running at 16 kHz against a model calibrated for 48 kHz, was miscalibrating speech probability — feeding bad AGC gating decisions and degrading the audio OWW received.

  **P0-1: Wake→turn dead zone removed (controller-only).** The most impactful single change. Previously, on wake word detection: controller sent `mic_stop` → drained queues → sent `mic_start(lock_mic:true)` → device tore down and restarted `streamMic`. Every sample spoken between wake-word end and the new VAD gate opening was lost — OWW chunk quantisation + inference latency + two WebSocket RTTs + fresh gate re-trigger. For a naturally spoken "Hey Jarvis turn on the lamp", the first word of the command fell in this hole, forcing the pause-and-enunciate behaviour. Fix: controller no longer sends `mic_stop`/`mic_start_turn` on wake. Sequence is now: wake detected → `oww_paused.set()` → routing flips from `mic_queue` to `voice_queue` — the stream was already running and the VAD gate was already open (that's how the wake word arrived), so command audio flows in with zero gap. `VOICE_PREROLL_DISCARD = 3` (240ms) discards the wake-word tail ("...Jarvis") from `voice_queue` before sending to HA. Controller-side 5s no-speech timeout replaces the device's `0x05` sentinel (which only arms on `lock_mic` streams). Button path retains `mic_stop`/`mic_start_turn` since there's no dead zone cost — button press happens before speech, so stop/start RTT is fine and directional lock is appropriate.

  **P0-2: Beamformer structural fix (device rebuild).** `BeamformingEnabled` flag previously controlled both smoother updates and output channel selection in `beamformer.Process()`. When disabled, smoothers froze — turning beamforming on mid-session gave cold baselines and garbage direction picks. Fixed by decoupling: smoothers always update regardless of flag; output channel is determined by lock state alone (unlocked → always ch6 omni, locked → selected perimeter mic); flag only gates whether `Lock()` does directional selection or no-ops. This means the baseline is always warm when beamforming is enabled. `Lock()` now takes `enabled bool` parameter; `Process()` signature drops the `enabled` parameter entirely. Button path sends `mic_stop`/`mic_start(lock_mic:true)` to engage directional lock — wake word path does not (stays on ch6 per P0-1). Beamforming is currently off by default: at typical conversational distances (≤1.5m) inter-mic SNR differences are marginal and the wrong-lock risk outweighs the directional benefit. Re-enable once the baseline audio quality issues (P0-3, P0-4) are properly addressed.

  **AGC identified as primary stability problem; disabled.** Instrumentation revealed AGC was the main culprit for the "works for a bit then stops" pattern: during idle periods, AGC drifted toward its 20× maximum gain chasing the -22dBFS target against near-silence. Amplified room noise crossed vadThreshold, filling `mic_queue` with noise frames, running OWW inference on continuous "speech" that wasn't — poisoning OWW's internal state until detection failed entirely. The gain state persisted across stream restarts (mic stream stops/starts on every TTS playback), so there was no natural recovery. AGC is now off by default. The AGC code remains and is re-engageable via dashboard toggle for A/B testing.

  **RNNoise (NS) identified as secondary problem; disabled.** RNNoise vendored model operates natively at 48 kHz. The pipeline feeds it 16 kHz audio — speech energy is squashed into the bottom third of the model's Bark bands, suppression decisions are miscalibrated, and consonant/HF content (exactly what STT needs) gets chewed. Additionally, the miscalibrated speech probability fed back into AGC gating was compounding the AGC drift problem. NS is now off by default. The RNNoise code remains and is re-engageable via dashboard toggle. Proper fix (P0-3) is to either resample 16→48 kHz around RNNoise or replace with a 16 kHz-native suppressor — deferred, but now clearly worth doing since disabling NS+AGC produced the best observed speech quality to date.

  **Pipeline toggles added (device + controller + dashboard).** `nsEnabled` and `agcEnabled` added as `*bool` fields to `config.go` `Device` struct, `ConfigMessage`, `Apply()`, `Snapshot()`, and `loadDefaults()`. Both default true (no behaviour change on upgrade; dashboard global config stores the actual values). `processor.go` `Process()` gains `agcEnabled bool` parameter — AGC block only runs when true, gain state preserved so re-enabling is smooth. `data.go` reads both flags from config snapshot each period. Dashboard advanced section: two new toggles ("Noise suppression (NS)", "Auto gain (AGC)"); VAD threshold slider floor dropped from 0.001 to 0.0001 to allow tuning to the actual measured signal levels.

  **VAD threshold corrected.** Dashboard slider floor was 0.001; measured conversational speech at 1.3m is 0.0004–0.0009; idle noise floor is 0.00017–0.00019. Default now 0.001 (down from 0.003–0.004), slider goes to 0.0001. With NS+AGC off, 0.001 gives reliable gate-open at normal voice levels with adequate noise margin.

  **Controller code quality fixes (no behaviour change).** B3: `DeviceESPhomeServer.stop()` now clears `_active_satellite = None` — previously a device bounce could leave a stale satellite reference causing `get_satellite()` to return a dead connection. B4: `satellite_server.py` dispatch now distinguishes handled-but-no-response from genuinely-unhandled messages via `_HANDLED` sentinel — `handle_message()` implementations yield `_HANDLED` for silent no-ops; only truly unrecognised message types log "unhandled". Four handlers in `em_esphome.py` updated (`SubscribeVoiceAssistantRequest`, `SubscribeHomeassistantServicesRequest`, `VoiceAssistantResponse`, `VoiceAssistantEventResponse`). `VOICE_PREROLL_DISCARD` moved to `em_esphome.py` module level (was a dead constant in `em_controller.py` doing nothing). Inline `import time` and `import em_controller` inside `_stream_mic_audio` replaced with proper module-level imports.

  **Per-turn structured trace added (controller).** `TurnTrace` dataclass in `em_esphome.py` collects timestamps at each pipeline stage (first audio frame, VAD end, STT result, TTS URL, TTS fetch, playback start, completion) and emits a single `[TURN]` log line at turn end. Example: `[TURN] trigger=wakeword(0.522) outcome=ok total=+9216ms first_frame=+257ms vad_end=+5973ms audio=74frames/5920ms stt=+7382ms text='What time is it?' tts_url=+7397ms tts_fetch=+8355ms tts_bytes=74880 playback=+8355ms`. Wake word turns carry the OWW score in the trigger label; button turns are labelled "button". Makes per-turn latency attribution possible without manual log reconstruction.

  **OWW near-miss score logging added (controller).** `wake_word_listener` now logs OWW scores above 0.05 at DEBUG level on every inference chunk. Critical for diagnosing "not responding" — distinguishes "score consistently 0.15–0.25, just below threshold" (tuning problem) from "score 0.01–0.03" (audio quality or pipeline problem). Previously only detections were logged.

  **Known working state as of this version.** NS off, AGC off, vadThreshold 0.001, beamforming off, vadSilenceMs 900. Responding reliably at conversational voice level from 1.3m in a quiet room. Lounge device (TV background) also confirmed working. The "must pause and enunciate" behaviour is gone.

  **Still open (deferred).** P0-3 proper NS fix: resample 16→48 kHz around RNNoise, or replace with a 16 kHz-native model. P0-4 AGC: if re-enabled, needs gain state reset on stream restart and lower max gain (current 20× is too aggressive). P0-5 device-side preroll ring. P0-6 OWW stream continuity (VAD-gated → continuous with zero-fill). Beamformer direction presets in dashboard (Front/Rear/All-round) advertise DSP the device implements but which isn't useful until P0-3/P0-4 are resolved and the baseline is solid.

- v2.6.4 — 2026-07-05: Bug fixes, conversation continuation, speaker stutter fix.
  **Bug: stuck LED ring on error/no-speech turns.** `on_thinking_esphome()` is scheduled via `asyncio.create_task()` at STT_VAD_END. On fast-exit turns (STT error, no-speech timeout), `cleanup_esphome()` ran and set `stop_spin` before the task actually executed. The task then created a new spinner task that nothing owned — LED ring spun forever with no wake or cancel able to clear it. Fixed by guarding `on_thinking_esphome` with `if stop_spin.is_set(): return` at the top — `cleanup_esphome` always sets `stop_spin` first, so this is an unambiguous "turn is over" signal.
  **Bug: TTS audio silently never played.** In `EchoMuseSatellite._handle_voice_event`'s `TTS_END` branch, `self._tts_audio_url = url` and `self._tts_event.set()` were incorrectly indented inside `if self._trace:`. The turn would unblock via RUN_END instead (after INTENT_END), find `_tts_audio_url = None`, log "No TTS audio URL received", and return silently — device said nothing, LED ring kept spinning. Fixed by moving both lines outside the `if self._trace:` block; only the timestamp recording belongs inside it.
  **Feature: HA-driven conversation continuation.** Wired up `continue_conversation` flag in `INTENT_END` data (confirmed present in logs since v2.6.0, previously discarded). `_handle_voice_event` now sets `self._continue_conversation = True` when flag is `'1'`. `trigger_voice_turn()` return type changed `None` → `bool`, returns the flag. `_run_voice_locked` continuation loop: if `should_continue` and not cancelled, drains stale frames, re-arms listening LEDs, loops back into `trigger_voice_turn` without clearing `oww_paused` or returning to OWW idle. Follow-up `VoiceAssistantRequest` is `start=True` with no `conversation_id` — HA threads conversation context server-side (confirmed from linux-voice-assistant source; the settle delay the reference uses after TTS is already covered by `_run_post_turn_playback`'s buffer drain sleep).
  **Fix: speaker mid-stream stutter.** `silenceLoop` in `pcm_speaker.go` uses a non-blocking `select` with a `default` silence-pump case. With `audioChanDepth = 4`, the channel could drain momentarily mid-stream (WebSocket jitter, goroutine scheduling), causing the `default` case to fire and inject a 42ms silence period — audible as a brief "CD skip" dropout. Fixed by raising `audioChanDepth` from 4 to 32 (~1.3s of headroom). Underrun instrumentation added to `silenceLoop` (`[speaker] underrun` log line) — remove once confirmed resolved across a few sessions.
  **Fix: dashboard offline IP display.** When a device is disconnected the dashboard was showing `127.0.0.1` — a Docker NAT artefact where `ws.remote_address` resolves to loopback when traffic comes through the Docker network, used as fallback if the device register message's `ip` field is missing. Fixed at the display layer: `127.0.0.1` treated as absent (shown as `—`); real IPs shown as `X.X.X.X (last seen)` in card subtitle and status tab when offline; small card badge shows `X.X.X.X ↑`. The DB write of `127.0.0.1` only occurs if the device doesn't send an `ip` field in its register message — devices do send it correctly, so this only affects early-registered devices and isn't worth correcting in the DB retroactively.
- v2.6.5 — 2026-07-06: Full implementation of the 2026-07-05 code review, plus follow-on fixes from live testing.
  **C1: HA VAD-end is the endpointing authority.** `_stream_mic_audio` previously had no exit except the device's own RMS-gate sentinel — in a noisy room the gate never closed and the turn (and spinner) hung indefinitely after HA had finished. Now an `_ha_vad_end` event (set on `STT_VAD_END` and `ERROR`) is raced against `voice_queue.get()` every iteration; the device sentinel remains advisory and still wins in quiet rooms. Whole streaming phase wrapped in a 20s hard cap; ffmpeg TTS decode capped at 15s (C1b, was unbounded).
  **C2: conversation continuation actually works.** The continuation loop's `finally` stopped the mic on every iteration but nothing restarted it before looping — continuation turns (shipped v2.6.4) silently timed out as no-speech every time. `mic_start()` now called in the continuation branch before looping.
  **C3: preroll discard is wake-turns-only.** The 240ms `VOICE_PREROLL_DISCARD` was applied to all turn types; button and continuation turns have no wake-word tail, so it just clipped their first word. `preroll_discard` is now an explicit parameter (wake: 3, button/continuation: 0), controlled by an `is_wakeword` flag rather than parsing the logging label. Dead duplicate constant removed from `em_controller.py`.
  **Regression fix: voice_queue drain race.** `oww_paused.clear()` ran before the post-turn drain, so ambient frames accumulated in `voice_queue` between turns and arrived as preamble on the next turn — first turn clean, every subsequent turn garbled (49–75 frames vs the normal 27–38). Drain moved inside `_run_voice_locked`'s `finally`, before the routing flip.
  **Acoustic-feedback guard: mic stops before TTS playback.** Previously only the post-turn `finally` stopped the mic, so the device processed 63–65 frames of its own TTS echo per turn, contended the Wi-Fi radio against incoming speaker frames (audible stutter), and crushed AGC gain. `mic_stop()` now sent immediately before playback in voice turns and around standalone announcements. Combined with the device-side `ResetAGC()`, this allowed **AGC to be re-enabled fleet-wide** (6/6 turns clean after re-enable).
  **Device: preroll ring (§3.4).** `streamMic` keeps the last 16 processed periods (~512ms) while the VAD gate is closed and flushes them upstream at gate open. Fixes the hard splice at speech onset that depressed OWW scores (real attempts measured 0.05–0.27 against the 0.3 threshold) and clipped first phonemes; also covers the continuation-turn gate-starts-closed wrinkle.
  **Device: ResetAGC at stream start.** AGC gain returns to unity on every mic stream start — a gain crushed by loud TTS echo (fast always-active attack, slow speech-gated release) previously persisted across streams and deafened the wake word for seconds.
  **Device: C4 config pointer race.** `Snapshot()` returned `&d.BeamformingEnabled` — a pointer into the mutex-guarded singleton, dereferenced by `streamMic` after `RUnlock` and racing `Apply()`. Copied to a local like the other fields.
  **Device: C5 (partial) — mute stops the stream.** Muting previously only refused *new* controller `mic_start` calls; an already-running stream kept sending audio while the ring showed red. The mute callback now calls `StopMic()`/`StartMic(false)`. Hardware half still open: ctls 105/106 mute chip A only; the on-device `tinymix -D 0` dump (now committed at `device/tools/tinymix_controls_output.txt`) confirms the B–D mute controls at 123/124, 141/142, 159/160 — adding them to `applyMute` is the next device rebuild item.
  **Device: B7 mutex encapsulation** — `SetOnMuteChange`/`SetOnVolumeChange` methods added so `Server` no longer reaches into the controllers' locks. **Q3** — misleading AGC/vadProb comments corrected (no behaviour change).
  **Device: speaker EOS vs underrun.** 0x03 EOS now calls `EndStream()` so `silenceLoop` logs "stream complete" instead of a false underrun when the audio channel drains at natural end of stream.
  **Q1: owwSpeexNs toggle.** openwakeword's built-in speexdsp noise suppressor (16kHz-native, wake path only) exposed as a per-device/global config toggle with live model reload; `speexdsp-ns==0.1.2` pinned in requirements (wheel confirmed installable against python:3.12-slim). Off by default pending an A/B wake-rate test in a noisy room.
  **Q2: default vadThreshold 0.003 → 0.001** — 0.003 sat above the measured conversational speech range (0.0004–0.0010 at 1.3m), so a fresh device or config reset failed to gate speech; 0.001 matches the validated v2.6.3 value and the dashboard fallback.
  **Q4: OWW near-miss visibility** — scores > 0.05 now log at INFO (rate-limited 1/2s per device) and increment a controller-owned counter shown on the dashboard status tab (kept out of `device.stats`, which the device's 30s hardware report overwrites).
  **M1** — button voice-turn task keeps a reference and logs exceptions via a done-callback instead of vanishing silently.
  **Misc controller** — `handle_data` queue-full now drops the oldest frame, not the newest (keeps the audio tail contiguous with real time); `_fetch_tts_audio` retries once (0.5s backoff) on intermittent tts_proxy fetch failures; idle OWW mic-queue timeout log demoted WARNING→DEBUG (fired every 10s per idle device); dashboard "omni" beam preset now sets `beamformingEnabled: false` (was `true`, which is AUTO perimeter-mic selection — not what the label promised).
- v2.7.0 — 2026-07-06: Ungated wake stream, mic-stream leak fix, noise-floor endpointing, mid-stream beam lock.
  **Ungated, AGC-free wake stream.** The always-on (!lock_mic) stream sends every 32ms period continuously (batched into 80ms frames) — no VAD gate, no preroll, no sentinels, AGC forced off regardless of config. OWW scores uninterrupted audio; no adaptive gain state can rebaseline against room noise (the root cause of the lounge device's wake death). VAD gate/AGC/preroll remain for bounded lock_mic (button) streams only.
  **Mic-stream leak fixed.** StopMic→StartMic pairs (sent after every turn) could spawn a replacement stream while the old goroutine drained; the old goroutine's defer then cleared micActive over the new stream, and the next mic_start spawned an unstoppable duplicate. Leaked gated streams duplicated all speech 2× and their VADEnd sentinels cleared the OWW buffer — the historical "wake degrades over days, reboot fixes it" root cause. Fixed by ownership check (`d.micStopCh == stopCh`) in the defer.
  **Controller-side noise floor + SNR endpointing.** Per-device asymmetric-EWMA noise floor (measurement only); esphome no-speech timeout restored to 5s, disarming only on SNR-relative speech or HA STT_VAD_START. beam_lock/beam_unlock control messages lock the beamformer at wake detection without a stream restart. Button path does mic_stop+mic_start post-turn so a gated turn stream can't persist as the wake stream.
- v2.7.1 — 2026-07-07: 24-bit fixed mic gain, PTY dashboard shell, log cap, state-aware landing page.
  **Fixed mic gain (`micGainDb`, default +24dB).** 20h of v2.7.0 fleet logs showed speech RMS at wake detection of 0.0001–0.0006 FS (~3–20 LSB in 16-bit) and a 6/19 empty-transcript rate: the S24→S16 extraction took the upper 2 bytes and discarded the low byte, where nearly all the signal lives at this hardware's capture levels. Gain is now applied to the full 24-bit sample (Q12 fixed-point, clamp to int16, clip counter) before quantisation — recovering real resolution rather than amplifying 16-bit quantisation noise. `vadThreshold` stays in pre-gain units (the device scales it by the linear gain internally), so stored configs never need retuning in lockstep. Validated on hardware: detection rms 0.0003 → 0.006–0.009, 5/5 clean transcripts, clipped=0. This is the "fixed gain" stage of the dumb-transducer target architecture.
  **PTY dashboard shell.** `shell_open` accepts `pty: true` (dashboard sessions only) — the device attaches sh to a real PTY (`/dev/ptmx` + `x/sys/unix` ioctls, `TERM=xterm-256color`); input is framed (0x00 stdin / 0x01 resize), output raw, controller proxies verbatim and announces the established mode via `shell_meta`. Dashboard terminal is vendored xterm.js 5.5.0 with a local-echo line-mode fallback for pre-PTY firmware. Programmatic sessions (OTA, `_shell_run`) keep the raw pipe.
  **/tmp/server.log cap.** Background trim loop in start_server.sh (5MB cap, newest 512KB kept in server.log.1; truncate-in-place is safe against the O_APPEND fd). A 45MB log was found on a device — /tmp is RAM-backed. Device VAD diag slowed from ~16s to ~10min cadence, with a prompt line whenever the clip counter moves.
  **State-aware landing page.** `/` now checks a stored session (→ /dashboard), then `GET /api/system/setup-state` (public, boolean only): first-run setup form with amber pulsing LED ring, or login form with green ring. `/setup` redirects to `/`. Sessions moved from sessionStorage to localStorage. The dashboard's internal Login component deleted — the landing page owns auth; logout and unauthenticated /dashboard visits redirect to `/`. SVG favicon (mini Echo ring) on both pages.
  **Config tab reorder.** Global + per-device config now run Playback → Wake word → Microphones → Advanced (turn processing + speech gate combined, "button turns only"); flow connectors dropped since order is by relevance, not signal path. All controls audited post-pipeline-changes: none dead; VAD threshold slider relabelled with pre-gain units.

- v2.7.2 — 2026-07-07: Beamformer lock-back selection.
  **Lock-back.** Controller wake detection lands 300–500ms after the wake word ends, so `Lock()`'s live onset ratio scored a decayed spike — the selected mic (and direction LED) was often unrelated to the speaker (the "known-poor selection" caveat since v2.7.0). The beamformer now records a ~2s ring of per-direction period energies (frozen while locked, like the baseline); `Lock()` scores each direction by the mean of its top-8 period energies within the window relative to its baseline, selecting on the recorded wake word rather than the present. Falls back to the live onset ratio when the ring is empty and raw energy when the baseline is cold. Unit tests added (`beamformer_test.go`, runnable in the compiler image with `GOOS=linux GOARCH=amd64 CGO_ENABLED=0` — the image cross-targets ARM by default). Known caveat: TTS echo enters the ring between turns (the baseline absorbs the same energy, damping its ratio); continuation-turn locks remain the weaker case until AEC.

- v2.7.3 — 2026-07-07: Acoustic echo cancellation (default off).
  **AEC.** speexdsp echo canceller (MDF, vendored SpeexDSP-1.2.1, float build, cgo like RNNoise) on the mono mic stream, right after beamformer+gain and before NS/AGC — the whole mic path including the wake stream. Far-end reference is tapped at the ALSA write in the speaker silence loop (every period *including silence*, so the reference clock advances in lockstep with playback), downmixed and 3:1 box-decimated 48k stereo → 16k mono, and buffered in a ring seeded with `aecDelayMs` of silence — modelling write-to-ear latency (speaker ALSA buffer ≈340ms). Both PCM devices share the codec clock, so ring occupancy cannot drift. Config: `aecEnabled` (default **false** — inert until enabled per deployment), `aecDelayMs` (default 250), `aecTailMs` (default 300); applied live on config push (echo state rebuilt on param change). Functional unit test drives the full WriteFar→ring→Process path with a synthetic aligned echo: 42dB attenuation, zero ring underruns (run with `GOOS=linux GOARCH=amd64 CGO_ENABLED=1` in the compiler image). Tuning guidance: enable on one device, speak during/after TTS playback; if residual echo persists, sweep aecDelayMs ±100ms (watch `[aec] reference underrun` — those indicate delay far too small); raise aecTailMs in reverberant rooms. Vendoring note: `_kiss_fft_guts.h` gained an include guard (kiss_fft.c + kiss_fftr.c share one cgo translation unit).

- v2.7.4 — 2026-07-07: Backlog quick fix-ups (C5 mute, §3.5, §3.6/B5, Q5).
  **Full-chip ADC mute (C5).** The mute button previously muted codec chip A only (ctls 105/106) — chips B–D, including ch6 (the mic OWW and STT actually use), stayed physically hot and the mic stream-stop was what made mute effective. All four confirmed pairs now toggle together; the red ring means hardware mute.
  **Beamformer buffer reuse (§3.5).** decode + band-diff analysis buffers allocated once in `New()` instead of ~24kB per 32ms period (~750kB/s GC pressure gone). `extractChannel` deliberately still allocates per period — data.go's preroll ring retains those slices.
  **VAD sentinel encoding (§3.6/B5).** The end-of-speech queue sentinel now carries its own type (`VAD_SENTINEL_END`/`VAD_SENTINEL_TIMEOUT` strings, defined in em_esphome.py; consumers accept legacy None defensively) — the old None + `device.last_vad_was_timeout` side-channel could have its flag overwritten by a second sentinel queued before the first was consumed.
  **Q5.** Speaker mid-stream underrun WARNING removed (clean since the v2.6.5 EOS disambiguation); dead legacy `Pump()` removed from the Speaker interface and implementation.

- v2.7.6 — 2026-07-07: Wake-word barge-in (default off).
  **Barge-in (§3.2).** Saying the wake word during TTS playback cancels the response and starts a fresh turn. Config `bargeInEnabled` (default false) + `bargeInThreshold` (default 0.6; effective threshold is max(barge, oww) so residual post-AEC echo can't self-trigger) — dashboard toggles live in the Wake word section. Mechanics: with barge-in on, `post_turn_play_esphome` skips the pre-playback `mic_stop` (the pre-AEC acoustic-feedback guard — safe now because device AEC subtracts the speaker output and AGC no longer exists on the wake stream) and runs `_barge_watcher`, which drains voice_queue (fed via oww_paused routing, otherwise unread during playback) and scores it with a dedicated per-device openwakeword instance (the main wake listener task is blocked awaiting the turn). Detection sets `barge_detected` + `cancel_event` — aborting `stream_speaker` and the drain sleep — and sends the new `speaker_flush` control message; the device discards its queued speaker periods (up to ~1.4s of buffered TTS; the ≤4 ALSA periods ≈170ms already in hardware still play). The turn loop then re-enters a fresh turn ("barge-in" trigger, wake-word preroll discard), keeping the mic running so words spoken in the same breath as the wake word survive. **Enable AEC first** — without it the watcher scores raw echo and the raised threshold is the only defence. Old device binaries log speaker_flush as unknown and let buffered audio play out (degraded, not broken). Standalone announcements (wizard/push TTS) remain non-interruptible.

- v2.7.7 — 2026-07-08: AEC actually works now; barge-in validated end-to-end; controller/device versioning split; HA-restart reconnect fix. A long root-causing session — four of the five bugs below masked each other, and every one produced the same symptom ("barge-in doesn't hear me").
  **ESPHome listeners never came back after an HA restart (controller).** Python 3.12 changed `asyncio.Server.wait_closed()` to block until all *accepted connections* finish, not just the listener. `DeviceESPhomeServer.stop()` (run on every device control-WS blip) closed the listener then parked forever while HA stayed connected; `device_connected()` saw `_server` still set and reported "already listening" against a dead port. When HA eventually restarted, the parked stops completed, the ports went down for good, and HA got connection-refused until the controller was restarted. `stop()` now detaches state before awaiting and closes the active satellite connection.
  **Controller/device versioning split.** Device firmware keeps plain `v*` tags (embedded in the binary, compared by OTA — unchanged). Controller releases use `controller-v*` tags → new `controller-release.yml` workflow → Docker image on `ghcr.io/wilbowes/echomuse-controller` (`X.Y.Z` + `latest`, CPU-only; **no GitHub Release created** — the OTA system polls repo releases for device firmware, and `_fetch_latest_release` is additionally hardened to filter for `v*`-tagged releases carrying a `server` asset). `controller/version.py` resolves the controller's own version (baked env → git describe → dev); surfaced in the dashboard header, `/api/system/status`, and as the ESPHome project version in HA. `requirements.txt` now defaults to CPU onnxruntime with a `GPU=1` Docker build arg for the CUDA swap; `docker-compose.deploy.yml` is the user-facing pull-and-run compose; quickstart/README lead with the prebuilt image.
  **AEC had never processed a single sample on hardware (v2.7.3–v2.7.6).** The load-bearing discovery: GoTinyAlsa's `GetAudioStream` reads `pcm_get_buffer_size` per chunk — the *whole* ALSA buffer (PeriodSize 512 × PeriodCount 5 = **2560 frames = 160ms**), not one period. The mic pipeline therefore runs on 160ms batches, and `aec.Process`'s `len(mono) != FrameSize*2` guard silently passed every buffer through untouched. Zero cancellation at every `aecDelayMs`, ring pegged at capacity, and — the cruel part — zero underruns or any other log to give it away, while the unit tests (single-frame buffers) showed 42dB. The v2.7.3 hardware "validation" (enabled + no underruns + clean turns) never had a chance of catching it. Fixes: `Process` handles any multiple of FrameSize (subframe loop); unsupported sizes are **logged loudly, never silently bypassed**; `TestHardwareShapedBuffers` drives real 2560-sample batches (45.8dB). Found via staged telemetry now kept permanently: `[aec] att=…dB mic= out= ref= ring=` ~1/s during playback and `[aec] far: rms= ring=` on the reference side.
  **Reference-ring staleness governor.** `WriteFar` fills the reference ring continuously (speaker silence loop) but `Process` stops with the mic stream, which restarts around every voice turn — each gap leaves unconsumed reference behind, and with equal produce/consume rates the backlog never drains; it compounds until the ring holds 3s-stale audio. An occupancy governor (post-consume low-water check) trims backlog beyond `delaySamples`+128ms and resets the filter; regression test simulates the mic gap.
  **`aecDelayMs` correct value is 0, defaults changed (was 250).** With the mic side reading 160ms batches, the capture path absorbs most of the speaker's write-to-ear latency; values ≥100 made the echo arrive *before* its reference (non-causal → zero cancellation, undetectable by the underrun counter, which only catches delay-too-small). Measured on hardware: converges to ~14dB per response at delay 0. Note the filter re-converges each turn — the beamformer locks a different channel per turn and each mic has a different echo path (per-channel filter states are the future fix).
  **Barge-in flush didn't actually stop playback.** `stream_speaker` writes the whole response into the WebSocket ahead of playback, so at barge time the remainder sits in TCP buffers on both ends; the device's `Flush()` drained its ~1.3s channel once and the WS reader refilled it — playback carried on after a skip, the interrupting turn open-mic'd the still-playing TTS, STT transcribed the assistant's own voice, and HA answered itself. Fixed device-side with a stateful flush (drain + **discard-until-EOS**: drop every subsequent 0x02 period until the stream's 0x03 arrives — immune to any amount of network buffering; a `streamActive` check keeps a flush racing a natural stream end from eating the next stream), plus controller-side guaranteed EOS: `stream_speaker` now sends 0x03 from a `finally` under `asyncio.shield`, since barge cancels the task mid-send and a stream ending without EOS would leave the discard armed against the next turn's audio.
  **Barge threshold semantics inverted — it must sit *below* the wake threshold.** The old `max(bargeInThreshold, owwThreshold)` floor guarded against echo self-triggering before AEC worked. Measured with working AEC: self-echo peaks 0.004 converged / 0.055 worst-case-unconverged, while a person talking over TTS scores only ~0.10–0.12 (the echo is ~25dB louder than the talker at the mic — speech-over-playback scores are inherently depressed). The max() is gone, `bargeInThreshold` is used as-is, default 0.10, dashboard slider floor lowered 0.3 → 0.05. Validated end-to-end: trigger at 0.104 → flush (33 periods + discard) → interrupting turn transcribed the user's actual words.
  **Barge re-entry re-arms listening LEDs** (the ring went dark while listening for the interrupting command — cleanup ran but the re-entry skipped the LED re-arm the continuation branch already had). *Known issue: LED state after barge-in is still not fully accurate — needs a dedicated pass.*

- v2.7.8 + controller-v2.8.1 — 2026-07-10: barge-in works in *real use* now, not just validation; mute and volume behave sanely mid-turn.
  **AEC filter no longer resets on reference resyncs — the real-world barge-in killer.** v2.7.7's validation passed, but a field test the same week failed completely (watcher peak 0.007 against the 0.10 threshold across 10s of the user repeating the wake word). Root cause: the mic ALSA ring is only PeriodSize 512 × PeriodCount 5 = **160ms deep**, so any stall of the capture chain longer than that silently loses whole 2560-sample batches at the hardware — and this happens every ~20–30s in steady state (confirmed: resync backlogs are always integer multiples of 2560 + phase). Each overrun left excess reference in the AEC ring; the occupancy governor trimmed it *and reset the speex filter* — including mid-playback — so the canceller lived in a permanent converge → reset → converge loop and never held more than ~5dB. The trim itself restores the exact alignment the filter converged against (both sides lose matching audio), so the learned echo path is still valid: the reset is now simply removed. `TestGovernorRecoversFromMicGap` post-gap attenuation went 22dB → **43dB** with the change. Live result: speech-over-TTS barge scores went 0.007 → 0.267–0.538, and converged self-echo peaks at 0.002–0.003 across consecutive turns — `bargeInThreshold` can safely sit at the 0.05 slider floor.
  **Mic capture-loss telemetry** (`pcm_microphone.go`): `[mic] capture stall:` logs any inter-batch gap >2× the batch duration (an overrun in progress, with estimated ms lost), a ~1/min `[mic] clock:` ledger tracks audio-received vs wall-clock (steady deficit growth = chronic loss; it also cleanly distinguishes overruns from clock-rate mismatch, which the first hour of data ruled out — deficit flat), and subscriber-queue drops are counted. Overruns are load-correlated (every ~5s during an OTA transfer); deeper ALSA buffering needs a GoTinyAlsa change first (per-period reads — `GetAudioStream` currently reads the whole buffer per chunk, so raising PeriodCount would also balloon the 160ms batch).
  **Post-playback drain sleep races cancel_event (controller).** `stream_speaker` finishes *writing* the response ~2× faster than it plays, so a barge-in usually lands during the buffer-drain `asyncio.sleep` — which nothing could cancel. Observed: device flushed instantly, controller hung for the remaining 5.7s of response length — no listening LEDs, and everything said in the window (including the wake word itself) piled into voice_queue, handing STT garbage like "Stop. Hang Rothsby, stop." The sleep now races cancel_event; barge → listening ring in well under a second and the interrupting turn carries only post-wake-word audio.
  **Volume arc survives active turns.** The turn animations repaint the ring continuously (~100ms cadence), so a volume press mid-turn showed the cyan arc for one frame — a glitch, not a reading. Controller LED frames now keep recording into `baseLEDs` during the arc's 2s display window but don't paint; on expiry the ring repaints the *latest* stored frame, handing back mid-animation with no dark gap. Idle behavior unchanged.
  **Mute terminates an active turn (controller) and the red ring is now actually sovereign (device).** Pressing mute mid-turn previously left the turn running against a silenced mic until it timed out. Now `mute_state(muted=true)` with the voice lock held cancels the turn exactly like the dot button, plus a `speaker_flush` so in-flight TTS goes silent immediately. That exposed a device gap: nothing actually blocked controller LED writes while muted (it never came up — turns couldn't overlap mute before); `SetLEDs`/`SetDirectionLEDs` now refuse to paint over the mute ring, making the long-documented sovereignty real. The cancelled turn's LED cleanup lands harmlessly in `baseLEDs`.

- 2026-07-11 (untagged, on top of v2.7.8): per-device WiFi change hardware-validated — three latent bugs found and fixed in one test session, each one a lesson in how differently the same commands behave inside the init-spawned Go binary vs an ADB shell.
  **The first "successful" switch never happened — `svc` is a shebang-less script.** `/system/bin/svc` starts with a `#` comment, not `#!`: a shell interprets it fine (which is why every provisioning-era test over ADB worked), but Go's `exec.Command` uses raw execve, which returns ENOEXEC — and `bounceWifi` discarded the error. WiFi never bounced, the supplicant kept its in-memory config, the SSID-blind `associated()` gate saw `wpa_state=COMPLETED` (for the *old* network) and waved the change through, and the false success **committed** — deleting the rollback backup while a garbage conf sat on disk. A reboot would have stranded the device; recovery was `wpa_cli save_config` (the running supplicant still held the working config in memory). Fixes: `svc` runs via `/system/bin/sh` with errors checked; the disable is *verified* to have dropped association before proceeding; the association gate requires the target SSID specifically, not just COMPLETED. Also fixed en route: `compile.sh` used bare `git describe --tags`, which picked up `controller-v*` tags — device builds now `--match 'v*'`.
  **The conf must be written while WiFi is down.** With the bounce actually working, the switch *still* failed — the supplicant rejoined the old network every time. On `svc wifi disable`, WifiStateMachine saves its in-memory network list back over `wpa_supplicant.conf`, clobbering anything written beforehand. The provisioning wizard's write-then-bounce order got away with it because a factory device has no framework-known networks to save; a provisioned device faithfully restores its current one. New order everywhere (change, revert, startup recovery): disable (verified) → write conf → enable. `associateTimeout` also raised 20s → 45s — only first-joins pay it; reverts re-associate to a known network well inside 20s.
  **Result delivery is now at-least-once.** The first genuinely-successful switch reported "timed out" to the dashboard: the switch kept the same IP, so the control WebSocket's TCP connection survived as half-open — `IsConnected()` said true, the single `wifi_result` send vanished into the dead socket, and the old take-and-send semantics meant the queued result was already consumed. The device now keeps the result pending and re-sends it (on reconnect + a 10s ticker) until the controller acks with `wifi_commit`, which the controller now sends for **every** wifi_result — for failures the marker/backup are already gone, so the ack is a harmless no-op; duplicate results are recorded/logged once.
  **start_server.sh fleet drift closed (same day, follow-up).** The startup script is installed at provisioning and had no update path afterwards — audit found Lounge a revision behind Office (missing the amp_off/idle-hiss fix; canonical md5 531dc3f2, Lounge d26fce30). Two fixes: Lounge got a one-off push (heredoc transfer, md5-verified, rename into place — takes effect on next reboot since the running shell holds the old inode), and every firmware OTA now syncs the device's script against the canonical `controller/device_payloads/` copy (`_sync_start_script`: md5 compare → skip when current; push + verify + rename when stale; best-effort, never blocks the firmware update). The binary-transfer helper was generalised to `_stream_file_to_device` for this — future payloads (BLE proxy etc.) ride the same path.
  All three safety paths validated on the Lounge device: rollback (garbage SSID — dropped, reverted, reported in 65s), startup recovery (marker found at boot → previous network restored + reported), and the happy path (Neptune-Secure → Neptune-Media in 30s, committed, no artifacts left). The WiFi tab's scan also works live (note: hidden SSIDs render as `\x00…` — cosmetic, unfiltered for now).

- 2026-07-11 (later session, untagged after v2.7.9): three quality-of-life fixes.
  **Barge-in now works during the thinking phase.** The watcher previously spawned only when TTS playback began, so a wake word spoken while the assistant was still thinking was buffered and then discarded — barge-in "only kicked in during the response" (user report). The watcher now starts at STT_VAD_END (thinking onset) and spans thinking → playback with a phase-dependent threshold: during playback it uses `bargeInThreshold` (speech-over-TTS scores are depressed ~25dB by the echo), during thinking — where nothing is playing and a 0.05 threshold would fire on random speech — it uses the device's normal wake threshold. Detection during thinking cancels the in-flight HA pipeline (`cancel_voice_turn`, same mechanism as mute/dot-button) instead of flushing a speaker that isn't playing; the turn loop's existing barge re-entry handles the rest. Watcher lifecycle moved to the turn loop's `finally` so every exit path stops it.
  **HA's wake-word dropdown no longer goes stale.** The ESPHome satellite advertises the device's wake word in `VoiceAssistantConfigurationResponse`, which HA requests only at connect time — and the advertised model was cached when the satellite server object was created, so dashboard wake-word changes never reached HA (it showed the provision-era model forever). `update_oww_model` now runs on every wake-word config change (per-device, global, and device-connect): it updates the stored id and bounces the active HA connection, which redials within seconds and re-reads the configuration. (The "Wake Word 2 = none" slot HA shows is normal — we advertise one model with `max_active_wake_words=1`; slots exist for satellites doing on-device multi-wake-word.)
  **Hidden SSIDs filtered from WiFi scan results.** wpa_cli renders hidden networks' zeroed SSIDs as literal `\x00\x00…` escapes, which showed up as garbage rows in the WiFi tab's network list. Scan now drops any entry that is entirely `\xNN` escapes — they're unjoinable by name anyway.

- 2026-07-12 (untagged): thinking-phase barge-in gained a second, lower detection tier. First real-world test of thinking-phase barge-in (three interruptions in succession) showed a genuine attempt scoring 0.240/0.242 on two consecutive frames against the single-frame wake threshold of 0.50 — missed, and the unwanted answer played in full. The watcher's thinking phase now fires on either a single frame at the wake threshold (as before) or two *consecutive* frames at `max(0.2, 0.4 × wake threshold)`. The consecutive-frame requirement is what keeps the low tier safe: random-speech near-misses observed in the logs are isolated single frames, while a real wake word sustains its score across frames. Playback-phase detection is unchanged (`bargeInThreshold`, single frame — echo depression already forces it low). A stream-reset sentinel clears the consecutive-frame history, so frames spanning a mic-stream restart can't pair up. Controller-only change, no device OTA needed.

- 2026-07-12 (later, untagged): the legacy `claracore` voice backend is gone. ESPHome/HA has been the fleet's only mode since 2026-07-06 and the claracore path (bespoke `VOICE_WS_URI` WebSocket exchange, `run_voice_turn`, the `VOICE_MODE` switch and all its branches) was unmaintained and already incompatible with the ungated wake stream. ~210 lines removed from `em_controller.py`; `.env` loses `VOICE_MODE`/`VOICE_WS_URI` (both ignored if still set). The ESPHome satellite path is now unconditional.

- 2026-07-12 (later still, untagged): P0-3 closed — noise suppression on the ASR path, done the way the architecture said it should be. The device's RNNoise stays dead (48kHz model, 16kHz audio — the original P0-3); instead the controller now runs DTLN (dual-signal LSTM, 16kHz-native, MIT, ~1M params, two ONNX models riding the existing onnxruntime dependency) on exactly one stream: the turn audio sent to HA's STT (`em_ns.py`, hooked into `_stream_mic_audio` behind the per-device `nsAsr` flag, default off, dashboard toggle in Microphones → advanced). The wake stream and the noise-floor measurement stay raw by design. Synthetic validation in-container: ~28dB attenuation on noise-only segments, −0.6dB on the speech band, 32× realtime on one CPU core (2.5ms per 80ms frame, run in the shared executor). Fail-open everywhere: missing models or a mid-turn inference error log a warning and stream raw. Models are vendored into the Docker image at build time pinned to a commit (bare-metal: `NS_MODEL_DIR`). Validation tooling: set `NS_DEBUG_DIR` and every denoised turn writes a raw/denoised WAV pair — listen to exactly what STT received. Expectation to hold it to: helps steady noise (fan/AC/hum) at marginal SNR — the "what's the time" → "bang bang" class of garble; does little against competing TV speech (that's the beamformer's fight).

- 2026-07-12 (device batch, untagged — binary `20260712-0155-dev`): two device changes riding together.
  **On-device RNNoise removed.** It never worked (48kHz-native model fed 16kHz audio — P0-3) and its replacement now lives controller-side (DTLN, `nsAsr`). Deleted `internal/rnnoise/` (vendored C + cgo bindings), the NS stage in `internal/processor`, and the `nsEnabled` config key end-to-end (device `ConfigMessage`, controller default, dashboard toggle). The RNNoise speech-probability interlock on AGC release went with it — it was dead code whenever NS was off (the shipped state for months), so AGC behaviour is unchanged: release gates on the RMS speech flag alone. Binary shrinks ~1MB. A stale `nsEnabled` in stored configs is harmless both directions (new firmware ignores unknown fields; old firmware keeps honouring the stored False until OTA'd).
  **Mute-button LED wired in.** The Dot has a discrete red LED under the mic-off button that stock Alexa lights when muted and EchoMuse never used. Recon via the shell proxy found it: it's not on the IS31FL3236 ring controller (all 36 channels = 12×RGB) but a bare GPIO — `/sys/class/gpio/gpio445/value` — confirmed by Amazon's own `libled_controller.so` symbols (`IssiLedDevice::setMuteButtonBrightness`, GPIO export path) and by toggling it live. New `internal/bindings/led/mute_button.go` (defensive export + direction, binary on/off); `muteController.applyMute/applyUnmute` drive it alongside the red ring, and startup init forces it off so a crash-while-muted can't leave a stale red button. Being GPIO-backed it needs none of the ring's repaint-suppression machinery.

- 2026-07-12 (follow-up, binary `20260712-0326-dev`): mute-button LED polarity was inverted — first field test showed no light on mute (and, per the electrical reality, the button would have been glowing while *unmuted*). Ground truth came from pulling `/system/lib64/libled_hal.so` off the device (base64 over the shell proxy) and disassembling `IssiLedDevice::setMuteButtonBrightness`: it streams **0** to `gpio445/value` for brightness > 47 and **1** for ≤ 36 — the line is active-low. (Same ELF confirmed `k_muteButtonGPIOAddress` = 0x1BD = 445, so the GPIO identification was right all along.) `SetMuteButtonLED` now writes 0=on/1=off, init parks it at 1.

- 2026-07-12 (BLE proxy, untagged — device + controller): the Dot becomes a Home Assistant **Bluetooth proxy** — a second use for hardware that was sitting dark. Per-device `bleProxyEnabled` toggle (dashboard stage 06, default off), feeding Bermuda room-presence and advert-based BLE sensors.
  **Hardware path (validated on Office before any code was written).** The MT8163's combo chip is exposed by MediaTek's WMT stack as `/dev/stpbt` — a raw H4-framed HCI char device; opening it powers the BT function on and downloads the firmware patch. It's single-owner, so enabling the proxy durably disables the Android Bluetooth stack (`pm disable com.android.bluetooth` + the Amazon csmbluetooth/bluetoothdfu packages + `settings put global bluetooth_on 0` — survives reboots; nothing EchoMuse uses needs Android BT). Validation from a raw shell: `autobt inquiry` (chip init + classic inquiry, found a neighbour), then hand-rolled HCI over `busybox printf`/`hexdump` — Reset → LE Set Scan Parameters (passive 100ms/50ms) → LE Set Scan Enable produced a live stream of LE Advertising Reports with the chip's **default event masks** (the vendor patch leaves LE Meta enabled — the Go init sequence stays minimal to match exactly what was validated).
  **Device** (`internal/bluetooth`, pure Go, no cgo): H4 stream reassembler + LE Advertising Report parser (unit-tested against bytes from the live capture), passive scan with HCI duplicate filtering off (Bermuda needs continuous RSSI), adverts coalesced per (address+payload) and batched (250ms/48-distinct) up the control WebSocket as `ble_adverts` JSON, watchdog re-init after 30s of HCI silence (mtk_stp_psm power-save insurance), scanner stats (+ chip BD address via Read_BD_ADDR) folded into the periodic stats message, scan-off + close on SIGTERM. Toggled live by config push — same `applyAecConfig`-style snapshot pattern.
  **Scan cadence was the load lever (found in the first live test).** The initial 100ms/50ms scan params — a 50% radio duty cycle — caught ~7× the advert volume of a normal passive scan; on this weak SoC (which already GC-stalls the mic every ~25s) the advert-processing load starved the control-WS goroutine into keepalive **ping-timeout disconnects** (Office dropped 3× in 8 min, felt sluggish). Fix: standard passive cadence **320ms/30ms** (matches ESPHome's `esp32_ble_tracker` default, ~9% duty), plus per-(address+payload) coalescing in the flush window so a beacon re-broadcasting identical data collapses to one advert carrying the latest RSSI — distinct payloads (ADV_IND vs SCAN_RSP) are preserved. After the fix: zero disconnects, a voice turn ran cleanly during an active scan (coex OK), ~1000 adverts/min forwarded to HA. Both knobs env-overridable (`BLE_SCAN_INTERVAL_MS`/`BLE_SCAN_WINDOW_MS`).
  **Controller** (`em_ble_proxy.py`): each enabled device gets a **second ESPHome device** — port is **voice satellite port + 1000** (16001 → 17001; deterministic, visibly paired, no separate allocator; v4 migration adds `ble_proxy_port`, persisted via `ensure_ble_proxy_port`), own mDNS entry (`echomuse-…-bt`), own MAC (the serial-derived MAC with the locally-administered bit flipped — deterministic and stable; the chip's real BD address is diagnostics-only, since it isn't known until the scanner first runs and changing HA identity later would orphan the registry entry). DeviceInfo advertises `bluetooth_proxy_feature_flags = PASSIVE_SCAN | RAW_ADVERTISEMENTS`; adverts forward as `BluetoothLERawAdvertisementsResponse` (msg 93 — already in the vendored protobufs). One diagnostic sensor (advert counter) — HA was observed to ignore zero-entity ESPHome devices. Deliberately separate from the voice satellite in HA, per design (HA discovered and subscribed to it as its own device — validated). Lifecycle is a single idempotent `reconcile()`: enabled+online → listener up; enabled+offline → mDNS only (HA shows unavailable); disabled → nothing exists. Dashboard: Bluetooth panel on the Status tab (scanner state, adverts seen, nearby-device count, HA link state, forwarded count).
  **Dashboard changes riding along:** each device's Status tab now shows its WiFi network name and ESPHome port; the voice-turn observability panel moved out of the cramped bottom of Status into its own **Activity** tab; and the **WiFi tab was folded into the top of the Config tab** as a distinct section above the fleet-inheritable config (WiFi is always per-device), removing the standalone WiFi tab.
- Released as device **v2.8.0** and controller **controller-v2.9.0** (the controller minor bump also rolls up the earlier claracore removal and DTLN NS work). Fleet OTA'd to v2.8.0.

- 2026-07-12 (deploy-all reliability): a fleet **deploy-all** updated one device fine but left the other stuck on "updating…". Root cause was **not** the OTA path — it was a latent SQLite concurrency bug. The controller shares one `sqlite3.Connection` across the `run_in_executor` thread pool (`check_same_thread=False`); `_tx()` writes held a lock but the read helpers (`_q`/`_q1`) took none. Deploy-all fires each device's `_run_update` concurrently, so one task's read raced another's write-commit on the same connection object → `SQLITE_MISUSE` ("bad parameter or other API misuse"), killing the second device's update before it even reached slot-detect. Solo updates never tripped it (only one task at a time). WAL's "concurrent readers" only applies across *separate* connections; on one shared connection every access must serialise — so reads now take the same lock (renamed `_write_lock` → `_db_lock`). Reproduced and confirmed fixed with an 8-thread × 500-cycle interleaved read/write stress test (was `SQLITE_MISUSE`, now clean). A general controller robustness fix, not just deploy-all.
  **Deploy-all is also resumable now.** The fleet deploy always ran server-side (per-device detached tasks — closing the modal never actually stopped it), but the only progress view lived *inside* the modal, so clicking out lost it and threw a React unmounted-update error. Deploy state now lives at the App level: a header pill ("Deploying vX — n/m" → "✓ Fleet on vX") persists across close and reopens the live per-device progress view; the modal makes clear updates continue in the background; the in-flight request is unmount-guarded.

- 2026-07-12 (oww_forge, untagged — new component): **custom wake-word trainer** at `oww_forge/`, a standalone Docker batch job (deliberately not part of the controller — ~25GB of training assets and a PyTorch image have no business in an always-on service). It containerises openWakeWord's official automatic-training pipeline: piper-sample-generator (LibriTTS-R, ~900 voices) synthesizes positives plus phoneme-overlap adversarial negatives, clips are augmented with MIT room impulse responses and AudioSet/FMA background noise, and a small classifier head trains against ~2,000 hours of precomputed ACAV100M negative features — output is a sub-megabyte `.onnx`, the exact format the controller's `OWWModel` already loads. `forge.py` wraps it in five subcommands (`assets` / `new` / `google-tts` / `build` / `test`), every stage resumable. Optional Google Cloud TTS layer mixes premium-voice positives (Neural2/Studio/Chirp, rate/pitch swept) into the training set for cross-family voice diversity at ~$0.50 per 2,000 clips. Upstream pins are load-bearing: piper-sample-generator is pinned to v2.0.0 (the last flat-layout release whose root-level `generate_samples.py` openWakeWord's `train.py` imports), openWakeWord to a verified main SHA with a one-line patch for its `--convert_to_tflite` argparse bug (string default `"False"` is truthy — every training run would otherwise end by importing TensorFlow, which the image deliberately omits; we ship ONNX only). Ships with a web frontend (`forge_web.py` + a single static page, aiohttp on port 8769, `docker compose up -d forge-ui`): asset checklist, wake-word cards with build/resume, a streaming job console, wav-upload scoring, and one-click `.onnx` download — jobs are forge.py subprocesses, one at a time, state re-derived from disk so the UI survives restarts. GPU: image builds CUDA 12.8 torch 2.7.1 — required for Blackwell cards (the RTX 5060 Ti is sm_120; notebook-era torch 2.1/cu121 can't see it at all), and carrying compiled kernels for everything Volta/Turing and newer — with automatic CPU fallback when no GPU is visible, plus a `forge-cpu` compose service for hosts without the nvidia runtime. Installing a finished model needs no controller change: `owwModel` accepts a file path, so drop the `.onnx` into the controller's data volume and point the config at it (dashboard model tiles are still a fixed list — API only for now; scan-and-merge dashboard integration sketched in `oww_forge/README.md`).
  **Validated end-to-end 2026-07-12/13** — first custom model (`hey_clara`) trained on the 5060 Ti: held-out synthetic positives score 0.86–0.94, noise controls 0.001. The first real runs surfaced five fixes, all landed: the AudioSet HF dataset had dropped its tar archives for parquet shards whose embedded metadata is too new for the pinned `datasets` (now read directly with pyarrow); the piper voice-config JSON lives in the generator repo's `models/` dir — which the image replaces with the /data symlink — so the assets step fetches it explicitly; containers get `shm_size: 8gb` (torch DataLoader workers SIGBUS at Docker's 64MB default); the `onnx` package rides along for `torch.onnx.export` (the first training run completed and then died at the final save); and deep-phonemizer's checkpoint load needed the same torch≥2.6 `weights_only=False` patch as piper — reached only for out-of-dictionary words, i.e. exactly the phonetic spelling variants below.
  **Accent & pronunciation support** (the training voices are American): comma-separated phrase variants train one model that fires on any spelling ("hey clara, hey clarra" covers the British reading — DeepPhonemizer maps 'clarra' → [K][L][AE][R][AH]); the Google TTS mix-in takes a language list (`en-GB,en-AU` for UK/Australian premium voices; usually free — a 2,000-clip run is ~2% of the monthly free premium quota); and the UI's "+ Family recordings…" uploads real household audio (any format, ffmpeg-converted, 1-in-10 held out for test) into the positive set, displacing synthetic clips — the strongest accent lever. Testing: "🎤 Try it" records from the browser mic and returns a plain verdict against the 0.5 wake threshold; file upload accepts any audio format.
  **UI restyled to the dashboard's design language** (greige chassis, gradient key-cap pill buttons, DM Sans/Mono, LCD-green inset console) and reworked for non-technical users: numbered steps, a per-word pipeline stepper (Created → Voices → Mixing → Ready) with a pulsing active stage, contextual primary action (Train/Continue/Retrain), accuracy boosters grouped with a "then retrain" nudge, and plain-language test verdicts with raw scores as small print.

*Device: Echo Dot 2nd Gen (RS03QR). Tested on macOS with ADB 35.0.2.*
