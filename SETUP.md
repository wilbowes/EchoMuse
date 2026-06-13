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

### Set up the binary directory:

```bash
adb shell "su -c 'mkdir -p /data/local/bin'"
adb push server /sdcard/server
adb shell "su -c 'cp /sdcard/server /data/local/bin/server && chmod 755 /data/local/bin/server && chown root:root /data/local/bin/server'"
```

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

> **`exec /data/local/bin/server`** — `exec` replaces the shell with the server process. Using `server &` causes the script to exit immediately, which init interprets as a crash and restarts the service every 5 seconds.

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
✅ OTA updates via controller dashboard — GitHub releases, on-device rollback
✅ Voice server turn timeout (45s) — controller never hangs on unresponsive voice server
✅ Boot logging to /tmp/server.log
✅ mDNS via grandcat/zeroconf — RFC 6762/6763 compliant, reliable discovery
✅ WebSocket protocol keepalives — dead connections detected within 30s
✅ Controller management dashboard — React SPA, vendored assets, no CDN dependency
✅ Dashboard live state — mute/listen/speak/offline via WebSocket events + 5s poll
✅ Dashboard shell terminal — browser-based root shell, Ctrl+C support
```

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
[0x04]                                                        — VAD end (speech finished)
```

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
- **Wyoming protocol** — upstream interface for Home Assistant / Rhasspy integration, turning EchoMuse into a proper HA voice satellite
- **Media player integration** — pause room audio on wake word, resume after response (Home Assistant `media_player` service call)
- **Bermuda BT proxy** — room-level presence detection via Bluetooth, once fleet of 5–6 Echo Dots is deployed
- **Adaptive VAD** — calibrate threshold on startup from ambient noise floor × multiplier. Currently fixed at 0.003; in very noisy environments this may need runtime adjustment.
- **RNNoise model upgrade** — vendored v0.1 model (2018). Newer models available via binary blob download; requires model loading API (rnnoise_model_from_file) present in newer source but needing the xiph.org CDN which was unavailable. v0.1 performs well for home environment use.
- **Startup chime** — short audio signature on EchoMuse init
- **Holding response** — play audio while Clara is thinking if response takes >2s
- **Browser-based provisioner** — WebUSB/ya-webadb installer for flashing new devices

---

**Document version:** v2.4.2
**Last updated:** 2026-06-13
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

*Device: Echo Dot 2nd Gen (RS03QR). Tested on macOS with ADB 35.0.2.*
