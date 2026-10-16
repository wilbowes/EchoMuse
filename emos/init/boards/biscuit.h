/*
 * Per-board constants for the Echo Dot Gen 2 ("biscuit").
 *
 * Included by init.c when the cmdline names this board (or names nothing,
 * which falls back to BOARD_DEFAULT). Every value here was read off a
 * running FireOS device rather than taken from documentation -- the
 * rationale lives at each constant, since that is what the next person
 * changing one needs to see.
 *
 * Adding a new board is "drop a boards/<name>.h with the same shape, add
 * one entry to the table in init.c". Nothing here knows what other boards
 * exist; the table does the picking.
 *
 * The off-target tools (cmdlinecheck, ringsim, pwcheck, tmoutcheck,
 * wpacheck, serialcheck) all #include "init.c" whole, so these defines
 * are visible to them too -- that is how cmdlinecheck exercises the
 * cmdline_board() parser. They are intentionally #defines, not static
 * const values, so a board that needs a different LEDDIR at compile time
 * can override via -D without rewriting this header.
 */
#ifndef EMOS_BOARDS_BISCUIT_H
#define EMOS_BOARDS_BISCUIT_H

/* ── Storage ─────────────────────────────────────────────────────────────── */

/* Cache partition: where the boot trail lives. Init writes a marker at
 * offset 0, /proc/version at 512, and the append-only trail at 1024 --
 * see note() and write_at(). A different board with a different cache
 * partition naming convention names its own path here. */
#ifndef BOARD_CACHE_PART
#define BOARD_CACHE_PART  "/dev/block/mmcblk0p15"
#endif

/* The boot partition the running image was flashed to. Init opens this
 * read-only to copy the byte-exact image into /data/emos/boot-good.img
 * (the rollback target), and write-only when it has to roll back.
 *
 * The path is fixed by the LK that ships with that kernel rather than by
 * us -- the kernel that boots biscuit puts boot_a at p10. A new board
 * would name whichever partition its own kernel uses. */
#ifndef BOARD_BOOT_PART
#define BOARD_BOOT_PART   "/dev/block/mmcblk0p10"
#endif

/* ── LED ring ────────────────────────────────────────────────────────────── */

/* The twelve-LED ring on biscuit is an is31fl3236 at i2c-0 0x3f, driven
 * by writing 12 RGB triplets as ASCII hex to its `frame` attribute --
 * the same interface the firmware's LED binding uses, so nothing here is
 * a new mechanism. The sysfs path below is the kernel's enumeration of
 * that device, on the 11007000.i2c bus that the MT8163 exposes.
 *
 * The path is board-specific because the i2c bus number and the LED
 * driver's sysfs layout are both board-defined. The other constants in
 * this section describe the geometry init's animator renders, all of
 * which were measured off the kernel's own orbit before this file was
 * written (see the comments in init.c around anim_claim). */
#ifndef BOARD_LED_NODE
#define BOARD_LED_NODE    "/sys/devices/soc/11007000.i2c/i2c-0/0-003f"
#endif

#ifndef BOARD_LED_COUNT
#define BOARD_LED_COUNT   12
#endif

/* The physical index that maps to logical position 0. The ring is
 * numbered 1-12 the way it physically sits; position 1 is just left of
 * 6 o'clock. The kernel's own orbit starts on physical 0, which is
 * seen as position 2 -- so we start at LED_BOTTOM=11 to align with the
 * gap just left of 6 o'clock. A ring with no gap at the bottom has no
 * LED_BOTTOM, and this value becomes a layout choice rather than a
 * measurement. */
#ifndef BOARD_LED_BOTTOM
#define BOARD_LED_BOTTOM  11
#endif

#ifndef BOARD_LED_DIR
#define BOARD_LED_DIR     1      /* +1: orbit runs on rising physical index */
#endif

/* The orbit's step period, measured off a running kernel rather than
 * chosen. Init's wind-in eases the head to its target on the same
 * period so the handover has nothing to reconcile -- any drift shows as
 * a stutter the very first time init takes the ring from the kernel. */
#ifndef BOARD_ORBIT_STEP_MS
#define BOARD_ORBIT_STEP_MS  109
#endif

/* ── Device nodes ────────────────────────────────────────────────────────── */

/* Every chrdev init creates by hand, in one place. There is no devtmpfs
 * on this kernel -- Android's /dev is a tmpfs populated by ueventd from
 * uevents, and we do not run ueventd -- so every node init needs is
 * mknod()ed from numbers read off a running device, never guessed.
 *
 * The firmware resolves input devices by NAME via /proc/bus/input/devices,
 * so the node numbering here only has to make them openable, not
 * meaningful: event2 is the volume button on biscuit and something else
 * entirely on other boards. A new board's table describes its own
 * enumeration -- the ALSA pcm minors, the input event mapping, the
 * combo-chip chrdevs.
 *
 * Each board header defines its own NODES_TABLE; init.c walks whichever
 * one the cmdline selected. The struct lives in boards.h so boards.c and
 * init.c agree on the layout without two declarations drifting apart. */
#include "boards.h"

static const struct board_node biscuit_nodes[] = {
    { "/dev/input/event0", 13, 64 },   /* ACCDET  */
    { "/dev/input/event1", 13, 65 },   /* mtk-kpd */
    { "/dev/input/event2", 13, 66 },   /* keys    */

    { "/dev/snd/controlC0", 116,  2 },
    { "/dev/snd/seq",       116,  1 },
    { "/dev/snd/timer",     116, 33 },
    { "/dev/snd/pcmC0D0p",  116,  3 }, { "/dev/snd/pcmC0D1c",  116,  4 },
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
    { "/dev/snd/pcmC0D21p", 116, 31 }, { "/dev/snd/pcmC0D21c", 116, 34 },
    { "/dev/snd/pcmC0D22p", 116, 35 }, { "/dev/snd/pcmC0D22c", 116, 36 },
    { "/dev/snd/pcmC0D23p", 116, 37 }, /* speaker */
    { "/dev/snd/pcmC0D24c", 116, 38 }, /* the 9-channel mic array */
    { "/dev/snd/pcmC0D25p", 116, 39 },

    /* MediaTek combo chip (WiFi + BT). wmtdetect is registered by the
     * kernel at boot; the other three chrdevs do not exist until
     * wmt_loader has detected the chip, but a node is only a pair of
     * numbers, so creating them up front is harmless and keeps all the
     * numbering in one table. */
    { "/dev/wmtdetect", 154, 0 },
    { "/dev/stpwmt",    190, 0 },
    { "/dev/wmtWifi",   153, 0 },
    { "/dev/stpbt",     192, 0 },
};

#endif /* EMOS_BOARDS_BISCUIT_H */
