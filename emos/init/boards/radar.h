/*
 * Per-board constants for the Amazon Echo 2 ("radar").
 *
 * Everything in this header was read off a rooted FireOS 6 device
 * (serial G2A0P308816702JC, board_id 0120 0014 0004 0017) and not from
 * documentation -- the values below are what the running kernel
 * enumerates, and trust the device before the datasheet when they
 * disagree. The same #ifndef / compile-time override pattern as
 * biscuit.h is used so a future radar variant can fix one constant
 * without rewriting this file.
 *
 * Notable differences from biscuit that show up here:
 *
 *   - Storage layout is A/B (boot_a / boot_b, system_a / system_b,
 *     lk_a / lk_b, tee_a / tee_b). Today init only knows one slot; the
 *     BOARD_BOOT_PART path below is the slot this image was built
 *     beside. The active slot is on the cmdline (ro.boot.slot_suffix)
 *     and a future change should read it there rather than hard-code.
 *
 *   - The LED ring is four TI LP55231 chips at i2c-0 0x32-0x35, NOT
 *     a single is31fl3236. The kernel does not bind the lp5523x driver
 *     by default on this build (/sys/class/leds is empty), so a future
 *     board bring-up needs to either bind it explicitly or talk raw
 *     I2C. Until then BOARD_LED_COUNT is 0 and the animator stays
 *     inert -- the firmware has more pressing problems than LEDs on a
 *     board that has never had a runtime.
 *
 *   - The audio stack is four TLV320AIC3101 ADC codecs on i2c-0
 *     (0x18, 0x19, 0x1A, 0x1B) for the 8-mic array plus one
 *     TLV320AIC32x4 DAC on i2c-2 for playback. The /proc/asound
 *     topology is therefore one ASoC card (`mtsndcard`) with ~26 PCM
 *     devices, same envelope as biscuit but with mic capture spread
 *     over the four ADC chips' I2S lanes.
 *
 *   - /proc/device-tree/compatible is just "mediatek,mt8163" -- no
 *     "amazon,puffin" prefix -- so the FDT compatible auto-detector
 *     in mkboot.py will not see this as radar. The board id has to be
 *     passed explicitly when packing the image. Acceptable: every
 *     board is identifiable to the packer through some other channel
 *     once it has any kernel-specific quirk, and FDT compatibility is
 *     not the right channel on this SoC.
 */
#ifndef EMOS_BOARDS_RADAR_H
#define EMOS_BOARDS_RADAR_H

/* ── Storage ─────────────────────────────────────────────────────────────── */

#ifndef BOARD_CACHE_PART
/* /cache exists on this device (756 MB, ext4). Same role as on biscuit:
 * the boot trail lives here. */
#define BOARD_CACHE_PART  "/dev/block/mmcblk0p15"
#endif

#ifndef BOARD_BOOT_PART
/* boot_a on radar. The current image was built beside slot A; flashing
 * to mmcblk0p10 overwrites the running boot. The active slot is on
 * the cmdline as androidboot.slot_suffix -- today this constant matches
 * it, and a future change should resolve from the cmdline instead. */
#define BOARD_BOOT_PART   "/dev/block/mmcblk0p10"
#endif

/* ── LED ring ────────────────────────────────────────────────────────────── */

/* Radar's LED ring is, like biscuit's, an is31fl3236 driver at i2c-0
 * 0x3F on the same MT8163 audiosys/i2c bus, with the same `frame`
 * sysfs attribute the firmware writes 72 hex chars to (12 LEDs × 3
 * bytes RGB) -- verified on a running FireOS 6 device, 2026-09-17.
 * The four LP55231 chips at i2c-0 0x32-0x35 are NOT the LED ring; they
 * are for backlight / proximity / IR (driver not bound on stock build
 * and likely unusable without a kernel rebuild, see JOURNAL 2026-09-17).
 * The is31fl3236 IS bound, and the firmware's led/i2c_controller.go
 * already hardcodes the same path on the device side, so init's
 * anim_claim() can take the ring over by writing "0" to boot_animation
 * and then frames straight to `frame`.
 *
 * Layout numbers -- LED_BOTTOM, LED_DIR, ORBIT_STEP_MS -- are picked
 * to match biscuit's known-good defaults. The kernel's own orbit was
 * not observable (Amazon's ledcontroller was killed mid-investigation
 * and did not resume animating), so these values are placeholders the
 * same way the analysis dump says: they need to be measured off a
 * running radar kernel before they ship. */
#ifndef BOARD_LED_NODE
#define BOARD_LED_NODE    "/sys/devices/soc/11007000.i2c/i2c-0/0-003f"
#endif

#ifndef BOARD_LED_COUNT
#define BOARD_LED_COUNT   12
#endif

#ifndef BOARD_LED_BOTTOM
/* Placeholder -- match biscuit's value so the first orbit is at least
 * in the same neighbourhood. To be measured. */
#define BOARD_LED_BOTTOM  11
#endif

#ifndef BOARD_LED_DIR
/* Placeholder -- +1 matches biscuit. To be measured. */
#define BOARD_LED_DIR     1
#endif

#ifndef BOARD_ORBIT_STEP_MS
/* Placeholder -- biscuit's measured value. To be measured on radar. */
#define BOARD_ORBIT_STEP_MS  109
#endif

/* ── Device nodes ────────────────────────────────────────────────────────── */

#include "boards.h"

static const struct board_node radar_nodes[] = {
    /* Input devices. The kernel enumerates:
     *   /dev/input/event0 = ACCDET (audio accessory detect, virtual)
     *   /dev/input/event1 = mtk-kpd (keypad, KEY_MUTE etc.)
     *   /dev/input/event2 = keys (gpio-keys, KEY_VOLUMEUP/DOWN only)
     * The firmware resolves by NAME, so the minor here only has to
     * make the node openable. */
    { "/dev/input/event0", 13, 64 },
    { "/dev/input/event1", 13, 65 },
    { "/dev/input/event2", 13, 66 },

    /* ALSA card 0 ("mtsndcard") -- same MT8163 audiosys topology as
     * biscuit, but the PCM device set differs slightly because the
     * external codec count is different (1 DAC vs 0, 4 ADCs vs 1). The
     * minors below mirror the kernel's enumeration on a live radar;
     * mknod'ing extras that the card never opens is harmless and
     * keeps the numbering in one place. */
    { "/dev/snd/controlC0", 116,  2 },
    { "/dev/snd/seq",       116,  1 },
    { "/dev/snd/timer",     116, 33 },
    { "/dev/snd/pcmC0D0p",  116,  3 },
    { "/dev/snd/pcmC0D1c",  116,  4 },
    { "/dev/snd/pcmC0D2p",  116,  5 }, { "/dev/snd/pcmC0D2c",  116,  6 },
    { "/dev/snd/pcmC0D3p",  116,  7 }, { "/dev/snd/pcmC0D3c",  116,  8 },
    { "/dev/snd/pcmC0D4p",  116,  9 }, { "/dev/snd/pcmC0D4c",  116, 10 },
    { "/dev/snd/pcmC0D5p",  116, 11 }, { "/dev/snd/pcmC0D5c",  116, 12 },
    { "/dev/snd/pcmC0D6p",  116, 13 }, { "/dev/snd/pcmC0D6c",  116, 14 },
    { "/dev/snd/pcmC0D7p",  116, 15 }, { "/dev/snd/pcmC0D7c",  116, 16 },
    { "/dev/snd/pcmC0D8p",  116, 17 }, { "/dev/snd/pcmC0D9c",  116, 18 },
    { "/dev/snd/pcmC0D10p", 116, 19 }, { "/dev/snd/pcmC0D11p", 116, 20 },
    { "/dev/snd/pcmC0D12c", 116, 21 }, { "/dev/snd/pcmC0D13c", 116, 22 },
    { "/dev/snd/pcmC0D14p", 116, 23 }, { "/dev/snd/pcmC0D15c", 116, 24 },
    { "/dev/snd/pcmC0D16c", 116, 25 }, { "/dev/snd/pcmC0D17p", 116, 26 },
    { "/dev/snd/pcmC0D17c", 116, 27 }, { "/dev/snd/pcmC0D18p", 116, 28 },
    { "/dev/snd/pcmC0D19p", 116, 29 }, { "/dev/snd/pcmC0D20p", 116, 30 },
    { "/dev/snd/pcmC0D21c", 116, 31 },
    { "/dev/snd/pcmC0D21p", 116, 34 }, { "/dev/snd/pcmC0D22p", 116, 35 },
    { "/dev/snd/pcmC0D22c", 116, 36 }, { "/dev/snd/pcmC0D23p", 116, 37 },
    { "/dev/snd/pcmC0D24c", 116, 38 }, /* TLV320AIC3101 8-channel mic */
    { "/dev/snd/pcmC0D25p", 116, 39 },

    /* MediaTek combo chip (WiFi + BT). Same major/minor allocation as
     * biscuit -- these are MT8163 SoC-level chrdevs and the bus is
     * identical, only the board changes. The connsys node at
     * 0x18070000 (vs biscuit's separate WMT/SDIO enumeration) is
     * abstracted behind board_wifi_up() in boards_radar.c; the node
     * numbers here are the same because the kernel module is the same
     * (mediated through the same /sys/class entries). */
    { "/dev/wmtdetect", 154, 0 },
    { "/dev/stpwmt",    190, 0 },
    { "/dev/wmtWifi",   153, 0 },
    { "/dev/stpbt",     192, 0 },
};

#endif /* EMOS_BOARDS_RADAR_H */
