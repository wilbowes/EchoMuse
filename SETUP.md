# Persistent Root on the Amazon Echo Dot Gen 2 (biscuit)

*A complete guide to rooting, SELinux bypass, Alexa removal, EchoGo installation, working speaker audio, VAD, wake word detection, and mute button — without tethered boot*

---

The Amazon Echo Dot 2nd Gen (codename: biscuit) has a small but dedicated hacking community. Most existing guides stop at tethered root — you get a root shell, but only while the device is connected to a computer running a patched preloader. Every reboot requires the cable.

This guide goes further. By combining the persistent amonet unlock with a boot image patch and a pre-seeded Magisk grant database, you get **persistent root that survives reboots** — no cable required after setup. Then we go further still and get EchoGo running as a proper init service with full hardware access including working speaker audio.

At the end you'll have:
- Full root via Magisk 17.3
- SELinux in permissive mode
- Alexa voice stack completely disabled
- EchoGo running on boot with full LED, mic, button, and speaker control
- Working audio output via TinyALSA directly (card 0, device 23)
- Energy VAD in EchoGo streaming speech bursts to the server
- OpenWakeWord wake word detection ("Hey Jarvis")
- Hardware mute button with LED feedback and action button lockout
- WiFi wake lock preventing FireOS from suspending the wireless interface



---

## Background & Credits

This builds on the work of:
- **R0rt1z2** — [amonet-biscuit](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/) persistent unlock and TWRP
- **Dragon863** — [EchoCLI](https://github.com/Dragon863/EchoCLI) tethered root research
- **Binozo** — [EchoGo](https://github.com/Binozo/EchoGo) SDK

The persistent unlock method (amonet-biscuit) is fundamentally different from the older tethered approach. EchoGo and similar projects assume tethered boot — this guide makes them unnecessary for the rooting phase.

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
  - `server` — compiled EchoGo server binary (ARM, API 22)

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

---

## Step 7 — Install EchoGo Server

EchoGo runs a Go HTTP server on the device that exposes the hardware over a simple API — LEDs, mic, speaker, buttons. The host controller connects over ADB forward.

### Set up the binary directory:

```bash
adb shell "su -c 'mkdir -p /data/local/bin'"
adb push server /sdcard/server
adb shell "su -c 'cp /sdcard/server /data/local/bin/server && chmod 755 /data/local/bin/server'"
```

### Create the startup script:

The script initialises the audio mixer, boosts mic gain, acquires a WiFi wake lock, and launches the server with `exec` (not `&`) so init sees it as a persistent foreground process rather than crashing immediately.

```bash
cat > /tmp/start_server.sh << 'EOF'
#!/system/bin/sh
sleep 60
iptables -I INPUT -i wlan0 -p tcp --dport 6996 -j ACCEPT

# Prevent FireOS from suspending the WiFi interface
echo "EchoGo" > /sys/power/wake_lock

# Speaker mixer init
tinymix -D 0 56 On
tinymix -D 0 64 1 1
tinymix -D 0 88 On
tinymix -D 0 61 100 100

# Mic gain — ADC_A channel 0, used for VAD and voice turns
tinymix -D 0 89 100 100
tinymix -D 0 92 60 60

/data/local/bin/volume_buttons.sh &
exec /data/local/bin/server
EOF
adb push /tmp/start_server.sh /sdcard/start_server.sh
adb shell "su -c 'cp /sdcard/start_server.sh /data/local/bin/start_server.sh && chmod 755 /data/local/bin/start_server.sh'"
```

> **`sleep 60`** — gives the network and audio HAL time to initialise before EchoGo starts. The iptables rule opens port 6996 on wlan0; FireOS blocks inbound connections by default. The WiFi wake lock (`/sys/power/wake_lock`) prevents FireOS from suspending the wireless interface, which would otherwise drop the WebSocket connection to the server after a few minutes of inactivity.

> **`tinymix 5 On` (Ext_Speaker_Amp_Switch)** is handled inside EchoGo's `pcm_speaker.go` Init() — no need to set it here.

### Add EchoGo and mixer service to the ramdisk:

The init scripts on FireOS 5 live in the boot image ramdisk, not in `/system/etc/init/`. We need to unpack the ramdisk, edit `init.csm.project.rc`, and repack.

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

Append the following two service blocks to the end of `init.csm.project.rc`. The `mixer` stub must come first — EchoGo's speaker Init() calls `stop mixer` as its first step, and this gives init something to stop:

```
service mixer /system/bin/sh
    oneshot
    disabled
    user root

service echogo /data/local/bin/start_server.sh
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
adb forward tcp:6996 tcp:6996
curl http://localhost:6996/
# Expected: "Echo up and running"
adb shell "su -c 'getprop init.svc.echogo'"
# Expected: running
adb shell "su -c 'getprop init.svc.mixer'"
# Expected: stopped
```

---

## Step 8 — Configure Audio for Speaker Playback

This is the critical step that isn't documented anywhere. The ALSA mixer is initialised with incorrect defaults — the external speaker amp and DAC are disabled. Without fixing this, tinyplay will open the PCM device and hang silently.

### Understanding the audio hardware

The biscuit uses a MediaTek MT8163 SoC with a TLV320AIC32x4 external codec. Speaker playback goes through ALSA card 0, **device 23**, at 48kHz stereo S16_LE, period size 2048, period count 4.

The mixer has 239 controls. Three are wrong at boot:

| CTL | Name | Default | Required |
|-----|------|---------|----------|
| 5 | Ext_Speaker_Amp_Switch | Off | **On** |
| 56 | Audio_I2S1_Setting | Off | **On** |
| 64 | HP DAC Playback Switch | Off Off | **On On** |

### Create the audio init script:

```bash
cat > /tmp/audio_init.sh << 'EOF'
tinymix -D 0 5 On
tinymix -D 0 56 On
tinymix -D 0 64 1 1
tinymix -D 0 61 100 100
EOF
adb push /tmp/audio_init.sh /data/local/tmp/audio_init.sh
adb shell "su -c 'chmod 755 /data/local/tmp/audio_init.sh'"
```

### Run it and test:

```bash
adb shell "su -c 'sh /data/local/tmp/audio_init.sh'"
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

You should hear a clean 440Hz tone. Ctrl+C to stop.

### Bake audio init into EchoGo startup:

Update the startup script to run mixer init before starting the server:

See Step 7 for the full `start_server.sh` — it already includes all mixer init, mic gain, WiFi wake lock, and server startup in one script.

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
✅ EchoGo running as init service on boot (exec mode, no crash loop)
✅ Dummy mixer service for EchoGo init compatibility
✅ Audio mixer configured at boot (tinymix in start_server.sh)
✅ Mic gain boosted — ADC_A digital volume 100, MICPGA 60 (channel 0)
✅ WiFi wake lock — FireOS cannot suspend wireless interface
✅ Full LED ring RGB control
✅ Microphone streaming (7 channels, S24_3LE, 16kHz, card 0 device 24)
✅ Speaker audio working (card 0, device 23, 48kHz stereo, period 2048 count 4)
✅ Button events
✅ WiFi working
✅ Stable boot
✅ EchoGo /speaker HTTP endpoint working — full pipeline confirmed
✅ Energy VAD — /vad_stream endpoint streams speech bursts only (silence dropped)
✅ OpenWakeWord — "Hey Jarvis" detected server-side, triggers voice turn
✅ Mute button — toggles mic mute (ADC_A), red LED ring, blocks action button
✅ Volume buttons — local interception, cyan LED ring feedback
✅ Amp boot click suppressed — mute/unmute around amp enable in pcm_speaker.go
✅ LED thinking spinner — triggered by THINKING signal from voice server when silence detected
✅ Preroll discard — first 320ms of mic stream discarded to avoid wake word bleed-through
✅ Speech threshold — recordings below peak RMS 30 discarded without hitting Whisper
```

---

## Key Files to Keep Safe

| File | Purpose |
|------|---------|
| `boot_patched.img` | SELinux-patched boot image |
| `magisk.db` | Pre-seeded root grant database |
| `Magisk-v17.3.zip` | Magisk installer |
| `f1r30s.zip` | ADB enablement patch |
| `update-kindle-csm_biscuit-272.6.8.0_user_680767620.bin` | FireOS 5 firmware |
| `server` | Compiled EchoGo server binary (ARM, API 22) |

If you need to reflash: Steps 2 → 3 → 4 → 5 → 6 → 7 → 8. Your saved `boot_patched.img` already contains the SELinux patch — no need to repatch from scratch.

---

## Audio Notes

**Why device 23?** The biscuit exposes 25+ PCM devices. Device 23 is the TLV320 DAC output path. Most other devices are modem/voice paths or internal DSP routes that hang or error on open.

**Why keep echoaudioservice?** The MediaTek audio DSP requires initialisation that happens inside Amazon's audio HAL. Without `echoaudioservice` running, the I2S clock never starts and `tinyplay` hangs indefinitely waiting for the hardware. The service itself is benign once Alexa's cloud components are disabled — it initialises the audio hardware and then sits idle.

**The mixer defaults are wrong.** This is the key insight missing from all other documentation. Three mixer controls must be set after every boot — the `start_server.sh` script handles this automatically. Without them, tinyplay hangs silently on device 23.

**The dummy mixer service is required.** EchoGo's speaker Init() calls `stop mixer` as its first step. On FireOS 5 there is no mixer service by default, so this call fails and Init() returns an error — silently swallowed by the server. Adding a dummy `mixer` service to init.rc allows `stop mixer` to succeed. This was discovered by comparing EchoGo's source code (`internal/bindings/speaker/pcm_speaker.go`).

**EchoGo /speaker endpoint — required fixes for biscuit.** The upstream EchoGo has three bugs that prevent audio on the biscuit:

1. **Period size** — upstream hardcodes 1024/2, device requires 2048/4. Fix in `internal/bindings/speaker/pcm_speaker.go`.
2. **HTTP handler reads in chunks** — the handler calls `Pump()` for each 4096-byte chunk read from the HTTP body, but `pcm_write` requires full period-aligned writes (8192 bytes). Fix: use `io.ReadAll` in the handler to buffer the entire body, then call `Pump()` once.
3. **Pump writes entire buffer at once** — `pcm_write` blocks if given more than one period at a time. Fix: write in 8192-byte (one period) chunks in a loop within `Pump()`.

These three fixes together produce clean audio. The `GoTinyAlsa` library also needs its `WriteFrames` error checking fixed — `pcm_write` returns 0 on success (not frames written), so the original `!= 0` check was correct but `pcm_writei` and `pcm_wait` are not available on this device's tinyalsa version.



**Mic gain — ADC_A only.** The 9-channel mic array uses four ADC pairs (A-D) in the TLV320 codec. Both the VAD stream and voice turn capture use channel 0, which maps to ADC_A. Only ADC_A needs boosting — controls 89 (Digital Volume, 88→100) and 92 (MICPGA, 40→60). At defaults the mic is too quiet for reliable wake word detection at normal speaking distance.

**WiFi wake lock.** FireOS aggressively suspends the WiFi interface during periods of inactivity, dropping the WebSocket connection to the server. Writing `"EchoGo"` to `/sys/power/wake_lock` prevents this — it's the same mechanism as Android's `WakeLock.acquire()`. The lock persists until the server process releases it or dies.

**Amp boot click suppression.** EchoGo's `pcm_speaker.go` Init() mutes the output (tinymix ctl 61 → 0), enables the amp (ctl 5 On), waits 50ms for it to settle, then unmutes. This eliminates the click that occurs when the TPA3118D2 powers up with an uncontrolled output.

**Mute implementation.** The mute button (KEY_MUTE, evdev code 113) arrives on `/dev/input/event1` (the dot device, not the volume device). Mute is implemented by setting ADC_A Left/Right Mute (tinymix ctls 105 and 106, BOOL type, 0=unmuted 1=muted). The mute controller in EchoGo intercepts the button locally, applies the tinymix change, updates the LED ring (red = muted), and signals the server to block dot button events.

**Voice server silence and speech detection.** The voice server uses absolute RMS thresholds rather than normalised ones. `SILENCE_THRESHOLD = 30.0` (absolute RMS) determines when recording stops. `SPEECH_THRESHOLD = 30.0` is the minimum peak RMS a recording must reach to be worth transcribing — recordings that never exceed this are discarded silently rather than sent to Whisper. This catches the common case of wake word detection triggering on noise with no speech following. `SILENCE_CHUNKS = 12` (~1 second) is the number of consecutive silent chunks before recording stops.

**Preroll discard.** The first 4 chunks (~320ms) of each voice turn mic stream are discarded before being sent to the voice server. This covers the tail of "Hey Jarvis" bleeding into the recording after wake word detection, which would otherwise cause Whisper to hallucinate.

**THINKING signal.** When the voice server detects silence and starts transcribing, it sends a `"THINKING"` WebSocket message to the echo_controller before the audio response. The echo_controller uses this to start the spinning LED animation immediately, giving visual feedback during the transcription + Clara + TTS pipeline rather than waiting until audio starts playing back.

**mDNS conflict handling.** If the echo_controller container restarts while the previous mDNS registration is still cached on the network (TTL 120s), it detects the conflict, queries for the existing registration, and reuses it if the IP and port match. This avoids a 2-minute wait on rapid restarts during development. In normal operation (container running all day) this never triggers.

**OWW model loading.** OpenWakeWord models are bundled inside the pip package at `/usr/local/lib/python3.12/site-packages/openwakeword/resources/models/`. Use `wakeword_model_paths` (not `wakeword_models`) in the `Model()` constructor with the full path including version suffix (e.g. `hey_jarvis_v0.1.onnx`). The prediction dict key is the filename without extension (`hey_jarvis_v0.1`). OWW threshold of 0.3 works well for a London/Bristol accent — the default 0.5 is calibrated for American English.

**Clicks between audio segments** are normal — the TLV320 mutes between playback sessions. This won't be audible with continuous audio streams.

**EchoGo crash loop prevention.** The init service must use `exec /data/local/bin/server` (not `server &`) so the shell script stays as the foreground process. Using `&` causes the script to exit immediately, which init interprets as a crash and restarts the service every 5 seconds, causing continuous noise from the amp cycling on and off.

---

## What's Next

The full voice pipeline is now working end to end:

```
"Hey Jarvis"
    → EchoGo energy VAD (Go, on-device)
    → /vad_stream HTTP endpoint (speech bursts only)
    → OpenWakeWord (server-side, Python)
    → wake detected → GET /microphone stream opens
    → S24_3LE 9ch → mono S16_LE conversion
    → WebSocket to clara-voice (Whisper large-v3, GPU)
    → silence detection → transcription
    → POST http://clara-bot:8766/message
    → Piper TTS (en_GB-alba-medium, 22050Hz)
    → resample 22050→48000Hz, mono→stereo
    → POST /speaker → TPA3118D2 amp → speaker
```

**Action button** triggers the same voice pipeline directly, bypassing wake word detection. Second press cancels at any stage.

**Mute button** mutes ADC_A (mic channel 0), shows red LED ring, and blocks the action button. Volume buttons remain active while muted (announcements can still play). Press again to unmute.

**What's next:**
- Holding response — play "let me look into that" when Clara takes more than ~2s to respond
- Startup chime — short audio signature on EchoGo init
- Volume feedback tone — beep at new volume level after button press
- On-device wake word — TFLite C binary running alongside EchoGo, eliminating the VAD stream entirely
- Custom wake word verifier — adapt the model to your voice/accent
- Speaker diarisation — identify which family member is speaking

The hardware is genuinely capable: 7-microphone array, full-RGB LED ring (IS31FL3236A controller, 12 RGB LEDs), hardware mute button, decent speaker driven by a TPA3118D2 class-D amp.

---

---

**Document version:** v1.3  
**Last updated:** 2026-04-27  
**Changelog:**  
- v1.0 — April 2026: Initial publication. Full pipeline confirmed working.  
- v1.1 — 2026-04-26: Fixed ambiguous init.csm.project.rc editing instruction (append not edit); fixed `server &` → `exec /data/local/bin/server` inconsistency in Step 8.  
- v1.2 — 2026-04-26: Updated start_server.sh (WiFi wake lock, iptables rule, mic gain, volume_buttons.sh); added VAD stream, OpenWakeWord, mute button, and amp click suppression; updated end state and What's Next.  
- v1.3 — 2026-04-27: Added THINKING signal, preroll discard, speech threshold, mDNS conflict handling, OWW model loading notes; updated end state and What's Next.

*Device: Echo Dot 2nd Gen (RS03QR). Tested on macOS with ADB 35.0.2.*
