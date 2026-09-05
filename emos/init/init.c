/*
 * First-boot init for biscuit: bring up a serial console over USB.
 *
 * Depends on nothing but the kernel — no shell, no shebang, no busybox, no
 * dynamic loader. An earlier busybox-script version produced no evidence at
 * all on three boots, which is indistinguishable from a kernel that never
 * started and cost most of a day to tell apart. Every step appends to a
 * progress trail on the cache partition, because on this device that trail is
 * the only diagnostic channel: no UART without opening the case, and the LED
 * ring is animated by the kernel's own is31fl3236 driver (its boot_animation
 * attribute, at i2c-0 0x3f) until userspace clears it, so it reports nothing
 * about our progress. It is not the LP55231 an earlier version of this comment
 * named — those probes fail with -6 on this board.
 *
 * ACM rather than adbd, deliberately. adbd needs functionfs descriptors and
 * Android's property service; with the gadget configured and adbd running,
 * android_usb sat at DISCONNECTED for ten seconds straight. f_acm needs no
 * daemon at all — the kernel exposes /dev/ttyGS0 and we put a shell on it.
 * The recipe is copied from the device's own init.mt8163.usb.rc.
 */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <signal.h>
#include <stdlib.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <stdint.h>
#include <sys/sysmacros.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define CACHE     "/dev/block/mmcblk0p15"
#define USBDIR    "/sys/class/android_usb/android0"
#define TTY       "/dev/ttyGS0"
#define TRAIL_OFF 1024


/* Every device node this system needs, created by hand.
 *
 * There is no devtmpfs on this kernel — Android's /dev is a tmpfs populated by
 * ueventd from uevents, and we do not run ueventd. Numbers were read off a
 * running FireOS device, never guessed. The firmware resolves input devices by
 * NAME via /proc/bus/input/devices, so the node numbering here only has to
 * make them openable, not meaningful: event2 is the volume button on biscuit
 * and something else entirely on other boards.
 */
struct node { const char *path; int major, minor; };

static const struct node nodes[] = {
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

    /* MediaTek combo chip (WiFi + BT). wmtdetect is registered by the kernel
     * at boot; the other three chrdevs do not exist until wmt_loader has
     * detected the chip, but a node is only a pair of numbers, so creating
     * them up front is harmless and keeps all the numbering in one table. */
    { "/dev/wmtdetect", 154, 0 },
    { "/dev/stpwmt",    190, 0 },
    { "/dev/wmtWifi",   153, 0 },
    { "/dev/stpbt",     192, 0 },
};

/* ── The boot progress ring ──────────────────────────────────────────────────
 *
 * The twelve-LED ring is an is31fl3236 at i2c-0 0x3f, driven by writing 12
 * RGB triplets as ASCII hex to its `frame` attribute — the same interface the
 * firmware's LED binding uses, so nothing here is a new mechanism.
 *
 * Until userspace clears `boot_animation` the kernel driver runs its own
 * orbiting animation: a dark blue ring with one lighter segment travelling
 * round it. That animation and the bootloader's ring before it are the only
 * things a user sees until we arrive, and it fights any frame we write — so
 * the ring is claimed as the FIRST thing init does, before any step that can
 * fail.
 *
 * What replaces it is deliberately a CONTINUATION of it rather than a
 * different display: the same two blues, a lighter head leading a dark blue
 * trail, in the same direction at the same rate. The head's position is the
 * boot's progress — twelve stages, one LED each, arriving back at the bottom
 * as the network comes up.
 *
 * The head moves continuously rather than in twelve jumps, and that is the
 * whole point of it. Stages are nothing like evenly spaced in time — linking
 * busybox applets is instant, fsck and WiFi association are seconds — so a
 * head that moved only on stage completion would sit motionless through
 * exactly the parts of the boot someone is most likely to read as hung. It
 * therefore eases towards the next stage and DECELERATES as it approaches,
 * never arriving until that stage really completes, and completing one gives
 * it a visible kick forward. It is never still, and it never claims progress
 * that has not happened.
 *
 * Rendering a fractional position on twelve LEDs is a cross-fade between the
 * two segments either side of it, which is also what makes it read as the same
 * object as the kernel's orbit rather than a chunky imitation of one.
 *
 * A stage that fails turns the head red and stops it. That is a clue, not a
 * diagnosis: the trail on the cache partition is the diagnosis.
 *
 * The animation runs in a FORKED CHILD holding the ring on its own ticker,
 * because everything it renders over — fsck, mounts, association — blocks PID
 * 1 for seconds at a time. The parent only ever publishes the stage it has
 * reached into shared memory; it never writes a frame while the child lives.
 */
/* Overridable so the ring animation can be rendered and checked off-target;
 * see emos/init/ringsim.c. */
#ifndef LEDDIR
#define LEDDIR "/sys/devices/soc/11007000.i2c/i2c-0/0-003f"
#endif
#define LED_N  12

/* Defined further down; declared here so the orbit probe and the stage marks
 * can record into the boot trail and the network log. */
static void note(const char *fmt, ...);
static void netlog(const char *fmt, ...);
static long mono_ms(void);

/* ── The orbit, as measured off EFF on 2026-09-05 ────────────────────────────
 *
 * Read straight out of the driver rather than eyeballed: `frame` is a
 * read/write attribute on Amazon's 3.18 driver too, so writing 1 to
 * boot_animation on a running device replays the orbit and each frame can be
 * read back exactly as the kernel is displaying it.
 *
 *   ground     0000ff on ALL TWELVE LEDs — a complete blue ring, not a dark
 *              one with a lit segment. This is the fact the design turns on:
 *              anything of ours that leaves LEDs dark is a hard cut at the
 *              handover, however well the colours match.
 *   head       00ffff — cyan. Not a lighter blue.
 *   direction  rising physical index, wrapping 11 -> 0.
 *   rate       109ms per step, min 100 max 120 over 13 clean transitions;
 *              1.31s per revolution.
 *
 * Sampling at 100ms first suggested exactly one step per sample, which is the
 * signature of aliasing rather than a measurement — hence the second pass at
 * full speed against /proc/uptime.
 *
 * LED_BOTTOM is settled differently: the ring's existing fill starts at the
 * LED just left of bottom centre and this one starts there too (Wil,
 * 2026-09-05). Twelve LEDs at 30° put none at dead bottom, so the bottom is
 * the GAP between positions 0 and 11, and the finale's arms are the pairs
 * either side of it.
 */
/* Physical index of logical 0 — the LED just left of 6 o'clock, which is
 * position 1 when the ring is numbered 1-12 the way it physically sits.
 *
 * It was 0, and that was one LED out: the takeover landed on position 2 rather
 * than position 1, and every arm of the closing sweep was rotated with it, so
 * the two sides met right of 12 o'clock instead of across it. Confirmed from
 * the device — the kernel starts its orbit on physical 0, which is seen as
 * position 2 (Wil, from video, 2026-09-05). */
#define LED_BOTTOM 11
#define LED_DIR    1        /* +1: the orbit runs on rising physical index */
#define ORBIT_STEP_MS 109   /* measured; the wind-in matches this exactly */
#define ORBIT_PROBE 1       /* sample the kernel's frames before claiming */

/* The orbit's ring, which is also what the tail repaints it with. */
static const unsigned char C_ORBIT[3]  = { 0x00, 0x00, 0xFF };
static const unsigned char C_HEAD[3]   = { 0x00, 0xFF, 0xFF }; /* the orbit's head */
static const unsigned char C_FAIL[3]   = { 0xFF, 0x00, 0x00 };
static const unsigned char C_AMBER[3]  = { 0xFF, 0x60, 0x00 };
/* Cyan is already full on two channels, so the only way UP is toward white —
 * adding red is what "brighter" means once green and blue are at 0xFF. */
static const unsigned char C_PEAK[3]   = { 0xFF, 0xFF, 0xFF };

#define SUB       256                  /* sub-LED position units */
#define TICK_MS   33                   /* ~30fps; 36 bytes of i2c per frame */
#define CREEP     ((SUB * 7) / 10)     /* how far into the next segment the
                                        * head may run while its stage is
                                        * still going. Under one whole LED, so
                                        * it can never reach a checkpoint the
                                        * boot has not actually passed. */
#define BREATH_MIN ((SUB * 13) / 20)   /* the head dips to 65%, never dark */

enum { ANIM_RUN = 0, ANIM_FAIL, ANIM_FINISH, ANIM_OFF, ANIM_DONE };

struct ledshm {
    volatile int stage;                /* stages completed, 0..LED_N */
    volatile int mode;
};
static struct ledshm *ledst;
static pid_t led_pid = -1;
static pid_t main_pid;
static int bootstep;

static const char HEXD[] = "0123456789ABCDEF";

static void puthex(char *o, unsigned char v)
{
    o[0] = HEXD[v >> 4];
    o[1] = HEXD[v & 15];
}

/* Write one frame, mapping LOGICAL positions (0 = bottom, rising the way the
 * orbit travels) onto physical LED indices. Every animation here is written in
 * logical space so the geometry lives in exactly one place.
 *
 * Note the hex is laid down two characters at a time rather than with
 * snprintf: the physical indices are not visited in order, so a terminating
 * NUL would land on the first character of a slot that had already been
 * written. */
static void led_write(const unsigned char f[LED_N][3])
{
    char hex[LED_N * 6];
    for (int i = 0; i < LED_N; i++) {
        int p = (LED_BOTTOM + LED_DIR * i) % LED_N;
        if (p < 0)
            p += LED_N;
        puthex(hex + p * 6,     f[i][0]);
        puthex(hex + p * 6 + 2, f[i][1]);
        puthex(hex + p * 6 + 4, f[i][2]);
    }
    int fd = open(LEDDIR "/frame", O_WRONLY);
    if (fd < 0)
        return;
    write(fd, hex, sizeof hex);
    close(fd);
}

static void led_solid(const unsigned char c[3])
{
    unsigned char f[LED_N][3];
    for (int p = 0; p < LED_N; p++)
        memcpy(f[p], c, 3);
    led_write(f);
}

/* out = a, with w/SUB of b laid over it. */
static void blend(unsigned char out[3], const unsigned char a[3],
                  const unsigned char b[3], int w)
{
    for (int i = 0; i < 3; i++)
        out[i] = (unsigned char)(a[i] + ((int)b[i] - (int)a[i]) * w / SUB);
}

static void scale(unsigned char out[3], const unsigned char c[3], int w)
{
    for (int i = 0; i < 3; i++)
        out[i] = (unsigned char)(c[i] * w / SUB);
}

/* A slow rise and fall, squared so it lingers dim and peaks softly rather than
 * pulsing evenly. This is what keeps the ring alive during a stage that takes
 * seconds: the head stops travelling as it approaches the next checkpoint, but
 * it never stops moving. */
static int breath(int tick)
{
    int per = 2400 / TICK_MS;
    int x = tick % per;
    int tri = x < per / 2 ? x * 2 * SUB / per : (per - x) * 2 * SUB / per;
    return tri * tri / SUB;
}

/* Where the head goes next, given the stage the boot has reached.
 *
 * Split out because ringsim USED to carry its own copy of this loop, so a cap
 * added here silently did not apply there — the drift the simulator exists to
 * make impossible. One function, called by both.
 */
static int head_advance(int head_q, int stage)
{
    if (stage > LED_N)
        stage = LED_N;
    int target = stage * SUB + (stage < LED_N ? CREEP : 0);
    /* Never past the LAST segment while the boot is still running. The creep
     * carried the head to 11.7, which renders mostly on position 0 — so it had
     * visually completed the lap and come back to the bottom with the boot
     * unfinished, and the finale then had no journey home to make. Bringing
     * the ring home is the finale's job (Wil, from video, 2026-09-05). */
    if (target > (LED_N - 1) * SUB)
        target = (LED_N - 1) * SUB;
    if (head_q >= target)
        return head_q;
    /* Exponential ease, with a floor so integer truncation cannot stall it
     * short of the target. */
    int step = (target - head_q) / 8;
    if (step < 1)
        step = 1;
    head_q += step;
    return head_q > target ? target : head_q;
}

/* head_q is a position in SUB units around the ring, 0..LED_N*SUB. `trail`
 * shows progress behind the head; during the wind-in there is none to show. */
static void anim_render(int head_q, int tick, int failed, int trail)
{
    unsigned char f[LED_N][3], head[3];

    /* The head breathes between the GROUND and the head colour rather than
     * toward black. Scaling cyan down takes the blue channel with it, so the
     * head would go dimmer than the ring it sits on; mixing keeps blue at full
     * and moves only the green, which reads as the head brightening and
     * settling rather than blinking. */
    if (failed)
        memcpy(head, C_FAIL, 3);
    else
        blend(head, C_ORBIT, C_HEAD,
              BREATH_MIN + breath(tick) * (SUB - BREATH_MIN) / SUB);

    /* Ahead of the head the ring is DARK, and the tail paints the orbit's own
     * blue back onto it as it goes. Carrying the lit ring through instead —
     * the first version of this — put the tail and the ground two shades of
     * the same blue apart, and on the device that read as no progress bar at
     * all (Wil, from video, 2026-09-05). The handover is covered by fading the
     * ring out rather than by keeping it lit; see anim_handover. */
    int i = head_q / SUB, fr = head_q % SUB;
    for (int p = 0; p < LED_N; p++) {
        if (trail && p < i)
            memcpy(f[p], C_ORBIT, 3);
        else
            memset(f[p], 0, 3);
    }

    /* The head straddles two segments, but it is NOT a plain cross-fade: the
     * nearer LED always gets the head at full and the further one takes a
     * proportional glow, widening to two full segments as it passes the
     * midpoint. A plain cross-fade splits the head's brightness in half there,
     * and at the bottom of the breath each half falls BELOW the trail — so the
     * leading edge stops being the brightest thing on the ring and the trail
     * reads as the head. Caught in ringsim, not on hardware.
     *
     * Modulo throughout, so a head arriving at LED_N lands back on the bottom
     * rather than off the end of the ring. */
    int near = fr < SUB / 2 ? i : i + 1;
    int far  = fr < SUB / 2 ? i + 1 : i;
    int glow = fr < SUB / 2 ? fr * 2 : (SUB - fr) * 2;
    blend(f[near % LED_N], f[near % LED_N], head, SUB);
    if (glow)
        blend(f[far % LED_N], f[far % LED_N], head, glow);

    /* Once the head reaches the last segment, the FIRST one lights with it and
     * the pair closes the ring across the bottom gap, breathing together until
     * the boot finishes. That wait is real and long — association and DHCP are
     * most of the boot — so it wants something deliberate rather than a head
     * sitting on its own. */
    if (trail && head_q >= (LED_N - 1) * SUB)
        blend(f[0], f[0], head, SUB);
    led_write(f);
}

/* Where the kernel's orbit had got to, in logical positions, so ours can carry
 * on from there instead of jumping to the bottom. The head is whichever single
 * segment is not the ground colour; anything else — a dark ring, a frame
 * already claimed by somebody, two lit segments — is not an orbit we can join,
 * and returns -1 to start at the bottom as before. */
static int orbit_head_pos(void)
{
    int fd = open(LEDDIR "/frame", O_RDONLY);
    if (fd < 0)
        return -1;
    char b[LED_N * 6 + 8];
    int n = read(fd, b, sizeof b - 1);
    close(fd);
    if (n < LED_N * 6)
        return -1;

    char want[6];
    puthex(want,     C_ORBIT[0]);
    puthex(want + 2, C_ORBIT[1]);
    puthex(want + 4, C_ORBIT[2]);

    int found = -1, count = 0;
    for (int L = 0; L < LED_N; L++) {
        int p = (LED_BOTTOM + LED_DIR * L) % LED_N;
        if (p < 0)
            p += LED_N;
        int same = 1;
        for (int k = 0; k < 6; k++) {
            char a = b[p * 6 + k], w = want[k];
            if (a >= 'a' && a <= 'f') a -= 32;   /* the kernel prints lower */
            if (w >= 'a' && w <= 'f') w -= 32;
            if (a != w) { same = 0; break; }
        }
        if (!same) { found = L; count++; }
    }
    return count == 1 ? found : -1;
}

/* Boot complete: close the ring, fill both sides from the bottom to the top,
 * take it to full and then take it away. The fade hands the LEDs back, so
 * anything shown afterwards is the firmware's to explain. */
static void anim_finale(void)
{
    unsigned char f[LED_N][3];

    /* Both sides, bottom to top.
     *
     * Numbering the ring 1-12 the way it physically sits: 1 is just left of 6
     * o'clock, 6 just left of 12 o'clock, 7 just right of 12 o'clock, 12 just
     * right of 6 o'clock. So there is no LED at dead bottom or dead top —
     * there is a GAP at each — and the balanced arms are the pairs either side
     * of the bottom gap: (1,12), (2,11) … meeting at the top gap between 6 and
     * 7. Zero-indexed here, so (0,11) up to (5,6).
     *
     * The progress head finishes on LED 12. This lights LED 1 alongside it
     * rather than TRAVELLING the head across the bottom gap to reach it, which
     * is what the first version did: that read as the head overshooting, and
     * started the sweep off balance (Wil, 2026-09-05).
     */
    for (int s = 1; s < LED_N / 2; s++) {
        for (int p = 0; p < LED_N; p++)
            memcpy(f[p], C_ORBIT, 3);
        /* 1 and 12 are already lit — the run ended on them — so the sweep
         * ADDS to that pair rather than starting from it: 2,11 then 3,10,
         * 4,9, 5,8 and finally 6,7 closing across 12 o'clock. */
        memcpy(f[0], C_HEAD, 3);
        memcpy(f[LED_N - 1], C_HEAD, 3);
        for (int k = 1; k <= s; k++) {
            memcpy(f[k], C_HEAD, 3);
            memcpy(f[LED_N - 1 - k], C_HEAD, 3);
        }
        led_write(f);
        usleep(70000);
    }

    for (int v = 0; v <= SUB; v += 16) {
        for (int p = 0; p < LED_N; p++)
            blend(f[p], C_HEAD, C_PEAK, v);
        led_write(f);
        usleep(12000);
    }
    for (int v = SUB; v > 0; v -= 10) {
        for (int p = 0; p < LED_N; p++)
            scale(f[p], C_PEAK, v);
        led_write(f);
        usleep(10000);
    }
    memset(f, 0, sizeof f);
    led_write(f);
}

/* The animator. Owns the ring for as long as it lives; reads the stage the
 * parent has reached and never writes it. */
/* Take the ring off the kernel, choosing WHEN rather than accepting wherever
 * the orbit happens to be.
 *
 * The animation runs until userspace stops it, so the moment is ours to pick:
 * wait for the orbit's head to reach the bottom and stop it exactly there.
 * Our head then starts where its head was, and the handover has nothing to
 * reconcile — no jump, and no winding the head round to the bottom afterwards,
 * which is what this replaces.
 *
 * It costs nothing, because it happens in the animator child while init gets
 * on with mounting /system. Bounded a little past one revolution so a ring we
 * cannot read is a plain takeover rather than a hang, and polled well inside
 * the 109ms step so the bottom is not missed.
 */
static void anim_claim(void)
{
    for (int i = 0; i < 100; i++) {
        int p = orbit_head_pos();
        /* Position 12 — the LAST segment, not the first. Taking it there means
         * our very next act is lighting position 1, which is the step the
         * orbit would have taken anyway, so the motion carries straight on
         * instead of repeating a position or starting a step late. */
        if (p < 0 || p == LED_N - 1)
            break;
        usleep(20000);
    }
    int fd = open(LEDDIR "/boot_animation", O_WRONLY);
    if (fd >= 0) { write(fd, "0", 1); close(fd); }
    fd = open(LEDDIR "/led_current", O_WRONLY);
    if (fd >= 0) { write(fd, "3", 1); close(fd); }
}

/* The handover: take the lit ring away, and leave the head behind.
 *
 * We stop the kernel's animation with its head at the bottom, so at this
 * instant the ring is exactly what it has been showing all along. Fading the
 * blue out from under that head — rather than cutting to a dark ring, or
 * carrying the lit ring through — is what makes the two animations read as
 * one: nothing jumps, nothing changes colour, the ring simply clears and the
 * head is left standing where the orbit left it, about to travel.
 *
 * It also buys the contrast the progress display needs. Against a lit ring the
 * tail was two shades of blue away from the ground and did not read as a
 * progress bar at all; against a dark one it paints the orbit's own blue back
 * on as it goes.
 */
static void anim_handover(void)
{
    unsigned char f[LED_N][3];

    for (int v = SUB; v >= 0; v -= 16) {
        for (int p = 0; p < LED_N; p++)
            scale(f[p], C_ORBIT, v);
        memcpy(f[0], C_HEAD, 3);      /* the head stays, and stays put */
        led_write(f);
        usleep(TICK_MS * 1000);
    }
}

static void anim_main(void)
{
    int head_q = 0, tick = 0;

    anim_claim();
    anim_handover();

    for (;;) {
        int mode = ledst->mode;

        if (mode == ANIM_OFF) {
            led_solid((const unsigned char[3]){ 0, 0, 0 });
            ledst->mode = ANIM_DONE;
            _exit(0);
        }
        if (mode == ANIM_FINISH) {
            anim_finale();
            ledst->mode = ANIM_DONE;
            _exit(0);
        }
        if (mode != ANIM_FAIL)
            head_q = head_advance(head_q, ledst->stage);
        anim_render(head_q, tick++, mode == ANIM_FAIL, 1);
        usleep(TICK_MS * 1000);
    }
}

/* Sample the kernel's own animation before taking the ring away from it.
 *
 * `frame` is read/write, so the driver hands back exactly what it is
 * displaying — which turns "match the orbit's blues" from an eyeballing job
 * into two constants copied off a boot trail, and the sample interval gives
 * its direction and period as well. Costs ~400ms of a boot; compile it out
 * once the numbers above are filled in. */
static void orbit_probe(void)
{
#if ORBIT_PROBE
    for (int i = 0; i < 10; i++) {
        int fd = open(LEDDIR "/frame", O_RDONLY);
        if (fd < 0) {
            note("orbit probe: frame unreadable errno=%d\n", errno);
            return;
        }
        char b[LED_N * 6 + 8];
        int n = read(fd, b, sizeof b - 1);
        close(fd);
        if (n <= 0) {
            note("orbit probe: read n=%d errno=%d\n", n, errno);
            return;
        }
        b[n] = 0;
        for (int k = 0; k < n; k++)
            if (b[k] == '\n')
                b[k] = 0;
        note("orbit %d %s\n", i, b);
        usleep(40000);
    }
#endif
}

/* Start the animator. It claims the ring itself — see anim_claim. */
static void led_claim(void)
{
    main_pid = getpid();
    orbit_probe();
    note("orbit head at %d\n", orbit_head_pos());

    /* The ring is NOT stopped here. anim_claim does it, in the child, once the
     * orbit reaches the bottom — so init is not held up waiting for it. */
    ledst = mmap(NULL, sizeof *ledst, PROT_READ | PROT_WRITE,
                 MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (ledst == MAP_FAILED) {
        ledst = NULL;
        note("led: mmap failed errno=%d — no boot ring\n", errno);
        return;
    }
    ledst->stage = 0;
    ledst->mode  = ANIM_RUN;

    led_pid = fork();
    if (led_pid == 0) {
        anim_main();
        _exit(0);
    }
}

/* One more stage done. Publishing the number is the whole of it — the head
 * eases there on its own ticker. */
static void led_step(void)
{
    if (bootstep < LED_N)
        bootstep++;
    if (ledst)
        ledst->stage = bootstep;
    /* Timestamp every stage, so the trail can answer which of them actually
     * cost anything. note() rewrites a buffer that fork() has copied, so the
     * network stages — which run in a child, and are the slow ones — have to
     * go to the append-only log instead. */
    if (getpid() == main_pid)
        note("stage %d reached\n", bootstep);
    else
        netlog("stage %d reached\n", bootstep);
}

static void led_fail(void)
{
    if (ledst)
        ledst->mode = ANIM_FAIL;
}

/* Hand the ring back. Bounded: the boot is never held up by an animation. */
static void led_end(int mode)
{
    if (!ledst)
        return;
    ledst->mode = mode;
    for (int i = 0; i < 60 && ledst->mode != ANIM_DONE; i++)
        usleep(50000);
}

#define led_finish() do { if (ledst) ledst->stage = LED_N; \
                          led_end(ANIM_FINISH); } while (0)
#define led_stop()   led_end(ANIM_OFF)

/* ── Boot rollback ───────────────────────────────────────────────────────────
 *
 * A boot image that starts init but then fails to become useful is the failure
 * this recovers, and it is the one we actually hit: an image that reached the
 * USB gadget and then crash-looped, needing TWRP and physical handling.
 *
 * The mechanism is deliberately ours and not the bootloader's. biscuit has a
 * real second slot (boot_b_x, mmcblk0p11) but LK chooses between them and we
 * have not reverse-engineered how — the standing guess is three tries per slot
 * and then a soft brick, which is plausible and UNTESTED. Building on that
 * guess would risk a device that boots something we did not choose, so instead
 * init keeps its own known-good copy on /data and restores it.
 *
 * Honest about what it does NOT cover: an image that fails before init runs
 * executes none of this and still needs recovery over USB. That case is what
 * the byte-exact packer and the cache-dropped flash verification exist to make
 * rare.
 *
 * A boot is CONFIRMED when the network comes up, not when init finishes.
 * Reachability is the property that matters — a device we can reach is one we
 * can fix without touching it.
 */
/* Defined further down; declared here so the rollback can use them. */
static void note(const char *fmt, ...);
static void netlog(const char *fmt, ...);
static int  readint(const char *path);

#define BOOTDEV   "/dev/block/mmcblk0p10"
#define GOODIMG   "/data/emos/boot-good.img"
#define BOOTSTATE "/data/emos/boot.state"
#define MAX_TRIES 3

static unsigned pad2048(unsigned n) { return (n + 2047u) / 2048u * 2048u; }

/* Length of the Android boot image on the partition, from its own header, so
 * only the image is copied rather than the whole 16MB partition. */
static long boot_image_len(int fd)
{
    unsigned char h[64];
    if (pread(fd, h, sizeof h, 0) != (ssize_t)sizeof h)
        return -1;
    if (memcmp(h, "ANDROID!", 8) != 0)
        return -1;
    unsigned ksz, rsz, ssz, ps;
    memcpy(&ksz, h + 8,  4);
    memcpy(&rsz, h + 16, 4);
    memcpy(&ssz, h + 24, 4);
    memcpy(&ps,  h + 36, 4);
    if (ps != 2048)
        return -1;
    return (long)ps + pad2048(ksz) + pad2048(rsz) + pad2048(ssz);
}

static int copy_range(int in, int out, long len)
{
    char b[65536];
    long done = 0;
    while (done < len) {
        long want = len - done < (long)sizeof b ? len - done : (long)sizeof b;
        ssize_t n = read(in, b, want);
        if (n <= 0)
            return -1;
        if (write(out, b, n) != n)
            return -1;
        done += n;
    }
    return 0;
}

static int read_state(void)
{
    return readint(BOOTSTATE);
}

static void write_state(int n)
{
    int fd = open(BOOTSTATE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0)
        return;
    char b[16];
    int k = snprintf(b, sizeof b, "%d\n", n);
    write(fd, b, k);
    fsync(fd);
    close(fd);
}

/* Write the known-good image back over the boot partition and reboot into it. */
static void restore_good(void)
{
    int in = open(GOODIMG, O_RDONLY);
    if (in < 0)
        return;
    struct stat st;
    if (fstat(in, &st) != 0 || st.st_size < 2048) { close(in); return; }
    int out = open(BOOTDEV, O_WRONLY);
    if (out < 0) { close(in); return; }
    int rc = copy_range(in, out, st.st_size);
    fsync(out);
    close(out);
    close(in);
    note("rollback restored rc=%d bytes=%ld\n", rc, (long)st.st_size);
    led_stop();                              /* the animator owns the ring */
    led_solid(C_AMBER);                      /* amber: rolling back */
    write_state(0);
    sync();
    sleep(2);
    reboot(RB_AUTOBOOT);
}

/* Keep the running image as the fallback, once it has proved itself. Only on
 * change, so an unchanged image costs no writes at all. */
static void promote_good(void)
{
    int in = open(BOOTDEV, O_RDONLY);
    if (in < 0)
        return;
    long len = boot_image_len(in);
    if (len <= 0) { close(in); return; }

    /* Compare by the boot header's SHA1 image id, not by size: every emOS
     * image built so far is byte-for-byte the same LENGTH, so a size check
     * would never promote a new one and the fallback would stay pinned to the
     * first image ever confirmed. The id is a digest over kernel and ramdisk,
     * so it changes exactly when the image does. It sits after the magic, the
     * ten header words, the 16-byte product name and the 512-byte cmdline. */
    unsigned char id_dev[32], id_good[32];
    if (pread(in, id_dev, sizeof id_dev, 576) == (ssize_t)sizeof id_dev) {
        int g = open(GOODIMG, O_RDONLY);
        if (g >= 0) {
            ssize_t n = pread(g, id_good, sizeof id_good, 576);
            close(g);
            if (n == (ssize_t)sizeof id_good &&
                memcmp(id_dev, id_good, sizeof id_dev) == 0) {
                close(in);
                return;                          /* already the known-good one */
            }
        }
    }
    if (lseek(in, 0, SEEK_SET) != 0) { close(in); return; }
    int out = open(GOODIMG ".new", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (out < 0) { close(in); return; }
    int rc = copy_range(in, out, len);
    fsync(out);
    close(out);
    close(in);
    if (rc == 0)
        rename(GOODIMG ".new", GOODIMG);
    else
        unlink(GOODIMG ".new");
    netlog("rollback promoted rc=%d bytes=%ld\n", rc, len);
}

static char trail[3072];
static size_t trail_len;

static int write_at(off_t off, const char *buf, size_t n)
{
    int fd = open(CACHE, O_WRONLY);
    if (fd < 0)
        return -1;
    ssize_t w = pwrite(fd, buf, n, off);
    fsync(fd);
    close(fd);
    return w == (ssize_t)n ? 0 : -1;
}

/* Milliseconds since the kernel started. CLOCK_MONOTONIC begins at boot, so
 * this covers the kernel's own time as well as ours and needs nothing set up.
 * It is not for the clock — it is for knowing which stages actually cost
 * anything, which the trail could not answer until now. */
static long mono_ms(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static void note(const char *fmt, ...)
{
    char line[256];
    int p = snprintf(line, sizeof line, "[%7ld] ", mono_ms());
    if (p < 0 || p >= (int)sizeof line)
        p = 0;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line + p, sizeof line - p, fmt, ap);
    va_end(ap);
    if (n < 0)
        return;
    n += p;
    if (n > (int)sizeof line - 1)
        n = sizeof line - 1;
    if (trail_len + n + 1 < sizeof trail) {
        memcpy(trail + trail_len, line, n);
        trail_len += n;
        trail[trail_len] = 0;
    }
    write_at(TRAIL_OFF, trail, trail_len);
}

static int wr(const char *path, const char *val)
{
    int fd = open(path, O_WRONLY);
    if (fd < 0)
        return -1;
    ssize_t n = write(fd, val, strlen(val));
    close(fd);
    return n > 0 ? 0 : -1;
}

static void usbwr(const char *leaf, const char *val)
{
    char p[256];
    snprintf(p, sizeof p, USBDIR "/%s", leaf);
    int r = wr(p, val);
    note("usb %s=%s rc=%d errno=%d\n", leaf, val, r, r ? errno : 0);
}

#define NETLOG "/data/local/tmp/net.log"

/* The WiFi stage runs in a forked child, so it must NOT use note(): fork copies
 * the trail buffer, and both halves would then pwrite divergent contents to the
 * same offset on the cache partition. /data is mounted by the time this runs. */
static void netlog(const char *fmt, ...)
{
    char line[256];
    int p = snprintf(line, sizeof line, "[%7ld] ", mono_ms());
    if (p < 0 || p >= (int)sizeof line)
        p = 0;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line + p, sizeof line - p, fmt, ap);
    va_end(ap);
    if (n <= 0)
        return;
    n += p;
    int fd = open(NETLOG, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0)
        return;
    write(fd, line, n > (int)sizeof line - 1 ? (int)sizeof line - 1 : n);
    close(fd);
}

/* fork+exec, returning the pid. NULL-terminated argv, argv[0] is the path. */
static pid_t spawn(char *const argv[])
{
    pid_t pid = fork();
    if (pid == 0) {
        char *envp[] = { "HOME=/", "ANDROID_ROOT=/system",
                         "PATH=/sbin:/system/bin:/system/xbin", NULL };
        int n = open(NETLOG, O_WRONLY | O_CREAT | O_APPEND, 0644);
        if (n < 0)
            n = open("/dev/null", O_RDWR);
        if (n >= 0) { dup2(n, 1); dup2(n, 2); if (n > 2) close(n); }
        int z = open("/dev/null", O_RDONLY);
        if (z >= 0) { dup2(z, 0); if (z > 0) close(z); }
        execve(argv[0], argv, envp);
        _exit(127);
    }
    return pid;
}

/* Read a small integer out of a sysfs file; -1 if it cannot be read. */
static int readint(const char *path)
{
    char b[32] = {0};
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return -1;
    int n = read(fd, b, sizeof b - 1);
    close(fd);
    return n > 0 ? atoi(b) : -1;
}

static int has_ip(const char *name)
{
    struct ifreq ifr;
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0)
        return 0;
    memset(&ifr, 0, sizeof ifr);
    strncpy(ifr.ifr_name, name, IFNAMSIZ - 1);
    int r = ioctl(s, SIOCGIFADDR, &ifr);
    close(s);
    return r == 0;
}

/* Run one command to completion and return its exit status. */
static int run_wait(char *const argv[])
{
    int st = 0;
    pid_t p = spawn(argv);
    if (p < 0)
        return -1;
    waitpid(p, &st, 0);
    return st;
}

/* Write /etc/resolv.conf pointing at the default gateway.
 *
 * The device has NO working DNS otherwise: FireOS keeps resolvers in Android
 * properties rather than a file, there is no /system/etc/resolv.conf at all,
 * and dhcpcd's "could not set property" lines are it failing to publish them
 * to a property service we do not run. Nothing noticed for a long time because
 * EchoMuse finds its controller over mDNS and connects by IP.
 *
 * The gateway is an ASSUMPTION, not a lease value: parsing dhcpcd's binary
 * lease would be the correct source, and on the overwhelming majority of home
 * networks the router that served DHCP is also the resolver. Stated here so
 * that whoever hits the exception knows exactly which line lied to them.
 */
static char gwip[32];

static void write_resolv_conf(void)
{
    FILE *f = fopen("/proc/net/route", "r");
    if (!f)
        return;
    char line[256], iface[32];
    unsigned dest, gw;
    while (fgets(line, sizeof line, f)) {
        if (sscanf(line, "%31s %x %x", iface, &dest, &gw) == 3 && dest == 0 && gw) {
            snprintf(gwip, sizeof gwip, "%u.%u.%u.%u",
                     gw & 0xff, (gw >> 8) & 0xff, (gw >> 16) & 0xff, (gw >> 24) & 0xff);
            break;
        }
    }
    fclose(f);
    if (!gwip[0])
        return;
    int fd = open("/etc/resolv.conf", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0)
        return;
    char buf[64];
    int n = snprintf(buf, sizeof buf, "nameserver %s\n", gwip);
    write(fd, buf, n);
    close(fd);
    netlog("resolv.conf nameserver %s\n", gwip);
}

/* Outbound-only packet filter, applied BEFORE the interface comes up.
 *
 * The device already holds no listening sockets at all — verified with
 * netstat, zero TCP and zero UDP — so there is nothing to connect to. This is
 * the second layer: it means a daemon that starts listening by accident, or a
 * future one added without thinking, is not reachable anyway. Stock FireOS
 * ships no rules whatsoever, so this is emOS being stricter than the thing it
 * replaces rather than catching up to it.
 *
 * Order matters: every ACCEPT is installed before the policy flips to DROP, so
 * the window where everything is dropped never exists. And it runs before
 * ifup, so the interface is never up without the policy.
 *
 * Four exceptions, each needed and each verified on hardware:
 *
 *   - lo, or anything talking to itself breaks.
 *   - ESTABLISHED,RELATED — the replies to our own outbound connections. This
 *     is what makes "outbound only" work at all.
 *   - udp sport 67 -> dport 68: DHCP offers are broadcast, so they match no
 *     connection and conntrack cannot help. Without this the device gets no
 *     address.
 *   - udp sport 5353: mDNS responses. The device FINDS ITS CONTROLLER this
 *     way, so dropping these is a device that boots perfectly and never
 *     connects to anything. Tested by killing the server and watching it
 *     rediscover: "mDNS: found Clara server at 10.10.1.81:8767".
 *
 * ICMP is allowed deliberately. It is not attack surface in any meaningful
 * sense and ping is the cheapest way to tell whether a device is alive — the
 * diagnostic value outweighs the theoretical purity of dropping it. Remove
 * this line if that trade ever stops being worth it.
 *
 * IPv6 gets the same treatment; ICMPv6 must be allowed or IPv6 cannot
 * function at all (neighbour discovery rides it).
 */
static void firewall(void)
{
    char *fw[] = { "/system/bin/sh", "-c",
        "for T in iptables ip6tables; do "
        "  $T -F INPUT; "
        "  $T -A INPUT -i lo -j ACCEPT; "
        "  $T -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT; "
        "  $T -A INPUT -p udp --sport 5353 -j ACCEPT; "
        "  $T -P INPUT DROP; "
        "  $T -P FORWARD DROP; "
        "done; "
        "iptables -I INPUT 4 -p udp --sport 67 --dport 68 -j ACCEPT; "
        "iptables -I INPUT 5 -p icmp -j ACCEPT; "
        "ip6tables -I INPUT 4 -p icmpv6 -j ACCEPT", NULL };
    int st = run_wait(fw);
    netlog("firewall applied status=%d\n", st);
}

static int ifup(const char *name)
{
    struct ifreq ifr;
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0)
        return -1;
    memset(&ifr, 0, sizeof ifr);
    strncpy(ifr.ifr_name, name, IFNAMSIZ - 1);
    int r = ioctl(s, SIOCGIFFLAGS, &ifr);
    if (r == 0) {
        ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
        r = ioctl(s, SIOCSIFFLAGS, &ifr);
    }
    close(s);
    return r;
}

/* Bring up WiFi, then supervise the two daemons that keep it up.
 *
 * This is Amazon's own sequence, read off a running FireOS device rather than
 * reconstructed: init.project.rc runs 6620_launcher, init.wifi.rc writes "1" to
 * /dev/wmtWifi, and wpa_supplicant/dhcpcd are started with the arguments the
 * running processes reported. Two steps had no counterpart in any rc file and
 * were found the hard way:
 *
 *   - wmt_loader must run FIRST. The stp/wmt/BT chrdevs are registered when it
 *     detects the chip over /dev/wmtdetect, so without it the majors simply do
 *     not exist and 6620_launcher sits idle forever with nothing to open.
 *
 *   - /etc must exist as a symlink to /system/etc. The kernel firmware loader
 *     searches /etc/firmware for WIFI_RAM_CODE, and our ramdisk has no /etc,
 *     so the combo chip powers on ("WMT turn on WIFI success!") and then
 *     wlanProbe fails on kalFirmwareOpen and no wlan0 is ever created. The
 *     symlink is the whole fix; it is not a WiFi thing at all, and anything
 *     else that goes looking under /etc or /vendor was equally broken.
 *
 * Writing "1" to /dev/wmtWifi blocks for ~13s while the chip is powered and the
 * firmware loaded, which is why this runs in its own process.
 */
static void net_main(void)
{
    char *loader[] = { "/system/bin/wmt_loader", NULL };
    char *launch[] = { "/system/bin/6620_launcher", "-p",
                       "/system/etc/firmware/", NULL };
    char *supp[]   = { "/system/bin/wpa_supplicant", "-iwlan0", "-Dnl80211",
                       "-c/data/misc/wifi/wpa_supplicant.conf",
                       "-e/data/misc/wifi/entropy.bin", NULL };
    char *dhcp[]   = { "/system/bin/dhcpcd", "-ABK", "-f",
                       "/system/etc/dhcpcd/dhcpcd.conf", "wlan0", NULL };
    int st = 0;

    waitpid(spawn(loader), &st, 0);
    netlog("wmt_loader status=%d\n", st);

    pid_t launcher = spawn(launch);
    sleep(2);

    int r = wr("/dev/wmtWifi", "1");
    netlog("wmtWifi=1 rc=%d errno=%d\n", r, r ? errno : 0);

    for (int i = 0; i < 20 && access("/sys/class/net/wlan0", F_OK); i++)
        sleep(1);
    firewall();                 /* before the interface is ever up */
    r = ifup("wlan0");
    netlog("wlan0 present=%d ifup=%d\n",
         access("/sys/class/net/wlan0", F_OK) == 0, r);
    bootstep = 9; led_step();                    /* 10: wlan0 exists */

    /* Supervise, driven by wlan0's carrier rather than by a fixed sequence.
     *
     * Two things were measured on hardware and neither is defensive coding:
     *
     *   - wpa_supplicant does NOT associate on its own here. Started fresh at
     *     boot it sat at wpa_state=DISCONNECTED for three minutes with its
     *     network enabled and unblocked; a single `wpa_cli reassociate`
     *     connected it in under ten seconds. So it gets nudged until carrier.
     *
     *   - dhcpcd must not start before association. Stock gets away with
     *     starting it early because Android's framework restarts it on link
     *     events; ours is given -K (ignore link messages) to match stock, so a
     *     dhcpcd started pre-association exhausts its attempts, keeps running,
     *     and never retries — a device associated to the AP with no address.
     *
     * dhcpcd is therefore started only once carrier is up, killed when carrier
     * goes away, and killed (to be respawned) if it has held carrier for 20s
     * without getting an address.
     */
    char *reassoc[] = { "/system/bin/wpa_cli", "-p/data/misc/wifi/sockets",
                        "-iwlan0", "reassociate", NULL };
    /* ntpd is pointed at the GATEWAY by IP, never at a hostname.
     *
     * bionic resolves DNS through Android's property service, not
     * /etc/resolv.conf, so a bionic-linked binary has no working resolver on a
     * device with no property service — busybox ntpd fails "bad address
     * pool.ntp.org" however correct that file is. (Our Go firmware is fine: Go
     * carries its own resolver and does read the file.)
     *
     * The gateway is the best available guess at a reachable time source and
     * many home routers serve NTP; where it does not, the clock stays wrong
     * and says so in this log rather than silently half-working.
     *
     * Client only. busybox ntpd SERVES time if given -l, and emOS holds no
     * inbound sockets at all — every daemon added here has to keep it that way.
     */
    char *ntpd[] = { "/system/bin/busybox", "ntpd", "-n", "-p", gwip, NULL };
    pid_t wpa = spawn(supp);
    pid_t dhc = -1, ntp = -1;
    int nudges = 0, dry = 0, netup = 0;

    for (;;) {
        pid_t d;
        while ((d = waitpid(-1, &st, WNOHANG)) > 0) {
            if (d == launcher)   launcher = spawn(launch);
            else if (d == wpa)   wpa = spawn(supp);
            else if (d == dhc)   dhc = -1;
            else if (d == ntp)   ntp = netup ? spawn(ntpd) : -1;
        }

        if (readint("/sys/class/net/wlan0/carrier") != 1) {
            if (dhc > 0)
                kill(dhc, SIGTERM);
            if (nudges++ % 3 == 0)
                spawn(reassoc);
            bootstep = 10; led_step();           /* 11: associating.
                                                  * Pinned rather than
                                                  * incremented: this is a
                                                  * supervision loop and may
                                                  * run many times. */
            dry = 0;
        } else {
            nudges = 0;
            if (dhc < 0) {
                dhc = spawn(dhcp);
                dry = 0;
            } else if (has_ip("wlan0")) {
                dry = 0;
                /* First address of this boot: DNS, then the clock.
                 *
                 * ntpd is CLIENT-ONLY and must stay that way — busybox ntpd
                 * serves time if given -l, and emOS listens on nothing. The
                 * device holds no inbound sockets at all (verified with
                 * netstat: zero TCP and zero UDP listeners), and every daemon
                 * added here has to preserve that.
                 *
                 * -q would set the clock once and exit, which loses it again
                 * on the next drift; -n keeps it in the foreground so the
                 * supervisor above owns its lifetime like everything else.
                 */
                if (!netup) {
                    netup = 1;
                    /* Fully up: the head comes home to the bottom, the ring
                     * fills from there to the top, brightens and fades. */
                    led_finish();
                    /* Ring handed back and the network is up: services that
                     * wait on the boot completing may now start. */
                    close(open("/run/net-up", O_WRONLY | O_CREAT, 0644));
                    /* The boot is confirmed: the device is reachable, which
                     * is the property that makes it fixable without hands. */
                    write_state(0);
                    promote_good();
                    write_resolv_conf();
                    ntp = spawn(ntpd);
                }
            } else if (++dry >= 4) {
                kill(dhc, SIGTERM);
                dry = 0;
            }
        }
        sleep(5);
    }
}

static void rdstate(const char *tag)
{
    char st[64] = {0};
    int f = open(USBDIR "/state", O_RDONLY);
    if (f >= 0) { read(f, st, sizeof st - 1); close(f); }
    for (char *c = st; *c; c++) if (*c == '\n') *c = 0;
    note("%s state=%s\n", tag, st);
}

/* Defined below, with the supervision machinery. */
static void svc_add(const char *name, char *const *argv, const char *req,
                    const char *after);
static void supervise(void);

int main(void)
{
    char buf[512];

    /* mknod's mode is masked by the umask, so without this every node below
     * comes out 0644 no matter what it asks for — which is how dhcpcd's hook
     * script ended up unable to open /dev/null for writing.
     *
     * It is restored to 022 as soon as the nodes exist, and that matters more
     * than it looks: umask is INHERITED by every child, so leaving it at 0
     * made PID 1 hand a permissive mask to every process on the device. The
     * syslog and anything a spawned shell created came out world-writable
     * (-rw-rw-rw-). Narrow the window to the thing that needs it. */
    umask(0);

    mkdir("/dev", 0755);
    mkdir("/proc", 0755);
    mkdir("/sys", 0755);
    int dtr = mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
    mount("proc", "/proc", "proc", 0, NULL);
    mount("sysfs", "/sys", "sysfs", 0, NULL);
    mkdir("/dev/block", 0755);

    /* Every device node is created by hand.
     *
     * This kernel has no devtmpfs — Android's /dev is a plain tmpfs populated
     * by ueventd from uevents, and we are not running ueventd. Nothing appears
     * on its own. The block nodes worked only because they were mknod'd
     * explicitly; /dev/ttyGS0 was assumed and therefore absent, which is why
     * the gadget reached CONFIGURED with nothing listening on it.
     *
     * 247:0 is ttyGS0, read off a running FireOS device rather than guessed.
     */
    mknod(CACHE, S_IFBLK | 0600, makedev(179, 15));
    /* boot_a_x, so emOS can reflash itself over the network: the device has
     * curl, busybox and dd, which turns a flash cycle from a TWRP trip into
     * about thirty seconds. It is also the recovery target, so verify any
     * write to it by dropping caches and reading BACK — a dd here can complete
     * at page-cache speed, verify against that same cache, and be lost on the
     * next reboot. Real writes to this eMMC run at about 9MB/s. */
    mknod("/dev/block/mmcblk0p10", S_IFBLK | 0600, makedev(179, 10));
    mknod("/dev/null",    S_IFCHR | 0666, makedev(1, 3));
    mknod("/dev/zero",    S_IFCHR | 0666, makedev(1, 5));
    mknod("/dev/tty",     S_IFCHR | 0666, makedev(5, 0));
    mknod("/dev/console", S_IFCHR | 0600, makedev(5, 1));
    mknod("/dev/ptmx",    S_IFCHR | 0666, makedev(5, 2));
    /* wpa_supplicant refuses to start without both of these. */
    mknod("/dev/random",  S_IFCHR | 0666, makedev(1, 8));
    mknod("/dev/urandom", S_IFCHR | 0666, makedev(1, 9));
    mknod(TTY,            S_IFCHR | 0600, makedev(247, 0));
    mkdir("/dev/pts", 0755);
    mount("devpts", "/dev/pts", "devpts", 0, NULL);
    mkdir("/dev/input", 0755);
    mkdir("/dev/snd", 0755);
    for (unsigned i = 0; i < sizeof nodes / sizeof nodes[0]; i++)
        mknod(nodes[i].path, S_IFCHR | 0600,
              makedev(nodes[i].major, nodes[i].minor));

    umask(022);   /* nodes exist; every child inherits a sane mask from here */

    snprintf(buf, sizeof buf, "EM64-INIT-OK acm-console\n");
    write_at(0, buf, strlen(buf));

    int fd = open("/proc/version", O_RDONLY);
    if (fd >= 0) {
        int n = read(fd, buf, sizeof buf - 1);
        close(fd);
        if (n > 0) write_at(512, buf, n);
    }
    note("stage=mounts done devtmpfs_rc=%d tty=%d\n", dtr, access(TTY, F_OK));
    led_claim();
    led_step();                                  /* 1: mounts and device nodes */

    /* /system read-only: this is a diagnostic boot and nothing here should be
     * able to damage the Android install we still rely on for recovery. */
    mkdir("/system", 0755);
    mknod("/dev/block/mmcblk0p13", S_IFBLK | 0600, makedev(179, 13));
    int r = mount("/dev/block/mmcblk0p13", "/system", "ext4", MS_RDONLY, NULL);
    note("stage=mount_system rc=%d errno=%d sh=%d\n", r, r ? errno : 0,
         access("/system/bin/sh", X_OK));
    if (r) led_fail(); else led_step();          /* 2: /system */

    /* /data read-WRITE: the firmware keeps config, wake-word models and logs
     * there. /system stays read-only — nothing here should be able to damage
     * the Android install we still rely on for recovery. */
    mkdir("/data", 0755);
    mknod("/dev/block/mmcblk0p16", S_IFBLK | 0600, makedev(179, 16));

    /* Check /data before mounting it read-write.
     *
     * This is the one partition emOS writes, and the one whose loss a remote
     * user cannot recover from. e2fsck is on /system (which is already mounted
     * by this point, read-only, so it is safe to run from). -p is preen mode:
     * fix only what is unambiguously safe and never ask a question, because
     * there is nobody at the console to answer one. Anything needing a real
     * decision is left alone and shows up in the trail.
     */
    char *fsck[] = { "/system/bin/e2fsck", "-p", "/dev/block/mmcblk0p16", NULL };
    int fs = run_wait(fsck);
    note("stage=fsck_data status=%d\n", fs);
    led_step();                                  /* 3: fsck /data */

    r = mount("/dev/block/mmcblk0p16", "/data", "ext4", 0, NULL);
    note("stage=mount_data rc=%d errno=%d\n", r, r ? errno : 0);
    if (r) led_fail(); else led_step();          /* 4: /data */

    /* Android's own root has these two symlinks and a surprising amount of
     * /system depends on them — the kernel firmware loader most of all, which
     * is what kept wlan0 from ever appearing. */
    /* /etc is a real directory of symlinks, NOT a symlink to /system/etc.
     *
     * Android's root symlinks it, and copying that costs the system a writable
     * /etc, because /system is mounted read-only and must stay that way. The
     * casualty is resolv.conf: FireOS keeps resolvers in properties and ships
     * no /system/etc/resolv.conf at all, so a symlinked /etc means the device
     * can never have working DNS. A symlink farm gives every /system/etc entry
     * at its expected path AND room to add our own files beside them.
     *
     * /vendor stays a plain symlink — nothing needs to write there.
     */
    mkdir("/etc", 0755);
    symlink("/system/vendor", "/vendor");
    DIR *ed = opendir("/system/etc");
    if (ed) {
        struct dirent *de;
        while ((de = readdir(ed))) {
            if (de->d_name[0] == '.')
                continue;
            char src[512], dst[512];
            snprintf(src, sizeof src, "/system/etc/%s", de->d_name);
            snprintf(dst, sizeof dst, "/etc/%s", de->d_name);
            symlink(src, dst);
        }
        closedir(ed);
    }
    note("stage=etc farm=%d\n", access("/etc/dhcpcd", F_OK) == 0);
    led_step();                                  /* 5: /etc */

    /* Preserve the previous boot's kernel log.
     *
     * MediaTek's ram_console publishes it at /proc/last_kmsg across a warm
     * reset, and it is the ONLY channel this device has for a crash: there is
     * no pstore on this kernel, and a fault that takes the box down leaves
     * nothing in any userspace log. It was what root-caused a spontaneous
     * reboot to a kernel data abort in the audio IRQ handler — but only
     * because someone happened to read it within two minutes, since the NEXT
     * reboot overwrites it. Copying it here means every crash survives for
     * whoever asks about it later, including a user on the other side of the
     * world who has since rebooted twice.
     */
    /* RAM-backed scratch for logs and runtime state, so routine logging never
     * touches the eMMC. 4MB is ample for a 256KB x2 syslog rotation. */
    mkdir("/tmp", 01777);          /* rootfs is a ramdisk: RAM-backed, as on stock */
    mkdir("/run", 0755);
    mount("tmpfs", "/run", "tmpfs", 0, "size=4m");

    mkdir("/data/emos", 0755);

    /* Rollback bookkeeping. This runs as early as /data allows, because a boot
     * that dies later must still have been counted — the counter is the only
     * evidence that the previous boots failed. */
    int tries = read_state();
    if (tries < 0)
        tries = 0;
    note("stage=boot try=%d\n", tries + 1);
    if (tries >= MAX_TRIES && access(GOODIMG, R_OK) == 0) {
        note("rollback: %d unconfirmed boots, restoring known-good\n", tries);
        restore_good();                 /* reboots; returns only on failure */
    }
    write_state(tries + 1);
    int lk = open("/proc/last_kmsg", O_RDONLY);
    if (lk >= 0) {
        /* Rotate, so a device that reboot-loops cannot overwrite the crash
         * that started it with the boring ones that followed. */
        rename("/data/emos/last_kmsg.1", "/data/emos/last_kmsg.2");
        rename("/data/emos/last_kmsg.prev", "/data/emos/last_kmsg.1");
        int out = open("/data/emos/last_kmsg.prev", O_WRONLY | O_CREAT | O_TRUNC, 0644);
        long long copied = 0;
        if (out >= 0) {
            char b[8192];
            int n;
            while ((n = read(lk, b, sizeof b)) > 0) {
                if (write(out, b, n) != n)
                    break;
                copied += n;
            }
            close(out);
        }
        close(lk);
        note("stage=last_kmsg bytes=%lld\n", copied);
    led_step();                                  /* 6: previous kernel log kept */
    }

    /* Put busybox's applets on PATH.
     *
     * /system ships busybox with 354 applets — vi, head, tail, sed, awk, less,
     * find, xargs, diff, stat, nc, wget, tar — but nothing symlinks them, so
     * every one has to be invoked as `busybox tail`, which is miserable over a
     * serial console and is most of what makes this device feel primitive.
     * Android's toolbox has none of them.
     *
     * Only applets that do NOT already exist in /system/bin or /system/xbin are
     * linked, so `busybox --install` is deliberately not used: /sbin comes
     * first on PATH, and installing all 354 would shadow toolbox's ps, mount,
     * id and kill with busybox versions that print different output. Nothing
     * the firmware execs (tinymix, stop, getprop, svc, pm) is a busybox applet,
     * and it stays that way by construction. 301 applets link on this build.
     *
     * A shell is used here, unlike everywhere else in this file, because by
     * this point /system is mounted and sh is the same binary we hand the
     * console. The no-shell rule exists for the early boot path, where a
     * broken interpreter is indistinguishable from a kernel that never ran.
     */
    mkdir("/sbin", 0755);
    char *link[] = { "/system/bin/sh", "-c",
        "for a in $(busybox --list); do "
        "  [ -e /system/bin/$a ] || [ -e /system/xbin/$a ] || "
        "    busybox ln -sf /system/bin/busybox /sbin/$a; "
        "done", NULL };
    int lst = 0;
    waitpid(spawn(link), &lst, 0);
    note("stage=applets status=%d vi=%d\n", lst, access("/sbin/vi", X_OK));
    led_step();                                  /* 7: busybox applets */

    /* Device mode: cmode 2 is charging-only, cmode 1 is device. */
    r = wr("/sys/devices/platform/mt_usb/cmode", "1");
    note("mt_usb cmode=1 rc=%d\n", r);

    usbwr("enable", "0");
    usbwr("idVendor", "1949");
    usbwr("idProduct", "2007");
    usbwr("f_acm/instances", "1");
    usbwr("functions", "acm");
    usbwr("bDeviceClass", "02");
    usbwr("enable", "1");
    led_step();                                  /* 8: USB gadget */

    for (int i = 0; i < 15; i++) {
        sleep(1);
        char tag[32];
        snprintf(tag, sizeof tag, "wait=%d tty=%d", i, access(TTY, F_OK));
        rdstate(tag);
        if (access(TTY, F_OK) == 0) {
            led_step();                              /* 9: console */
            break;
        }
    }

    /* WiFi is a supervised service like everything else — see svc_add below.
     * It is NOT forked here.
     *
     * It was, once, and the leftover fork survived the move to the supervisor,
     * so two WiFi stages ran concurrently: two wmt_loaders, two 6620_launchers
     * and two `echo 1 > /dev/wmtWifi` power-ons racing each other on the
     * MediaTek combo chip. The device crash-boot-looped. That is the same
     * subsystem whose firmware assert took a kernel down on 2026-09-04, so
     * racing its power-on is not a small thing to get wrong.
     */

    /* Local logging. Both of these are LOCAL ONLY and must stay that way:
     * busybox syslogd forwards to a remote host with -R and listens with -R
     * plus -L, and emOS holds no inbound sockets at all. klogd feeds the
     * kernel ring into the same file so a userspace fault and the kernel
     * message that explains it land in one timestamped stream. */
    /* Routine logging goes to a RAM ring, NOT to flash.
     *
     * This device is meant to run for years on eMMC that cannot be replaced,
     * and a syslog writing continuously is pure wear for logs nobody reads:
     * measured at ~19 bytes/s idle, so ~1.6MB a day, forever. -C gives an
     * in-memory circular buffer instead, read with `logread`, costing zero
     * writes. (A console session inflates that rate a hundredfold, because
     * this kernel logs one line PER BYTE transmitted on ttyGS0 — so the
     * measurement above is the idle device, not a device being debugged.)
     *
     * A tmpfs is used rather than busybox's own -C ring, which needs System V
     * shared memory this kernel does not have — `logread` returns "can't find
     * syslogd buffer: Function not implemented". tmpfs is present and gives
     * the same property: the log lives in RAM and costs no writes.
     *
     * Flash is reserved for post-mortems, which is the thing actually worth
     * keeping: the previous boot's kernel log is copied at startup, and
     * dump_log() spills the ring on the way down. Crashes survive; chatter
     * does not.
     */
    char *syslogd[] = { "/system/bin/busybox", "syslogd", "-n",
                        "-O", "/run/messages", "-s", "256", "-b", "2", NULL };
    char *klogd[]   = { "/system/bin/busybox", "klogd", "-n", NULL };

    /* EchoMuse itself, via its OWN supervisor rather than directly.
     *
     * start_server.sh owns the A/B slot symlink, the fast-exit backoff and the
     * log trimming that the OTA system depends on, so init starting the binary
     * would quietly bypass firmware rollback. Android's init launched the same
     * script; emOS does the job it used to. It is absent on a device where
     * EchoMuse has not been installed, which is an ordinary state, not a fault.
     */
    char *echomuse[] = { "/system/bin/sh", "/data/local/bin/start_server.sh",
                         NULL };

    svc_add("net", NULL, NULL, NULL);       /* forked in-process, see below */
    svc_add("syslogd", syslogd, NULL, NULL);
    svc_add("klogd", klogd, NULL, NULL);
    /* EchoMuse waits for the network stage to finish, and the wait is about
     * the LED RING as much as connectivity. The firmware claims the ring the
     * moment it starts, so a server starting mid-boot drives the same twelve
     * LEDs as the boot progress bar and the two fight — the identical
     * two-writer conflict, over the same i2C device, that the kernel's
     * boot_animation caused. Waiting for /run/net-up means the ring has
     * finished and faded before the firmware touches it, and EchoMuse has a
     * controller to reach when it does start. */
    svc_add("echomuse", echomuse, "/data/local/bin/start_server.sh",
            "/run/net-up");
    svc_add("console", NULL, NULL, NULL);   /* needs the tty as its stdio */

    supervise();
    return 0;
}

/* ── Supervision ─────────────────────────────────────────────────────────────
 *
 * PID 1 has three jobs and the first version of this init did only one of
 * them: it supervised a console, never reaped orphans (so anything that
 * reparented to init became a permanent zombie), and had no shutdown path at
 * all — reboots went through `reboot -f`, which leaves /data mounted
 * read-write with unflushed writes. ext4's journal covered for that, but a
 * corrupted /data is precisely the failure a remote user cannot recover from.
 */

#define MAX_SVC 8
struct svc {
    const char  *name;
    char *const *argv;   /* NULL for the two services started specially */
    const char  *req;    /* must exist for the service to run; NULL = argv[0] */
    const char  *after;  /* wait until this exists; unlike req, keep waiting */
    pid_t        pid;
    time_t       started;
    int          fails;  /* consecutive fast exits */
    int          gone;   /* logged as not-installed; stop trying */
};
static struct svc svcs[MAX_SVC];
static int nsvc;

static volatile sig_atomic_t want_shutdown;

static void on_term(int sig) { (void)sig; want_shutdown = 1; }

static void svc_add(const char *name, char *const *argv, const char *req,
                    const char *after)
{
    if (nsvc < MAX_SVC)
        svcs[nsvc++] = (struct svc){ .name = name, .argv = argv, .req = req,
                                     .after = after, .pid = -1 };
}

/* How long to wait before restarting a service that keeps dying.
 *
 * A service that exits immediately used to be restarted every 2s forever,
 * which spins PID 1, fills the log with its own failure, and buries whatever
 * else was wrong. Anything that survives FAST_EXIT is treated as a normal
 * run and resets the count; repeated fast exits back off 2,4,8,16,32,60s and
 * stay at a minute. It never gives up permanently, because the reason is
 * usually transient on this hardware — a PCM still held, a partition not
 * mounted yet — and a device that has quietly stopped trying is worse than
 * one that is still retrying slowly.
 */
#define FAST_EXIT 10
static int svc_backoff(int fails)
{
    if (fails <= 1)
        return 2;
    int b = 2 << (fails - 1);
    return b > 60 ? 60 : b;
}

/* The console is the one service that needs a controlling terminal, and it
 * cannot be started until the host has attached to the ACM gadget. A console
 * that exits once because nobody was listening yet is indistinguishable from
 * one that never worked, which is why it is respawned rather than run once. */
static pid_t start_console(void)
{
    int t = open(TTY, O_RDWR | O_NOCTTY);
    if (t < 0)
        return -1;
    pid_t pid = fork();
    if (pid == 0) {
        setsid();
        ioctl(t, TIOCSCTTY, 1);
        dup2(t, 0); dup2(t, 1); dup2(t, 2);
        if (t > 2) close(t);
        char *argv[] = { "/system/bin/sh", NULL };
        char *envp[] = { "HOME=/", "TERM=vt100", "ANDROID_ROOT=/system",
                         "PATH=/sbin:/system/bin:/system/xbin", NULL };
        execve("/system/bin/sh", argv, envp);
        _exit(127);
    }
    close(t);
    return pid;
}

/* Stop everything, flush, and let the kernel reset.
 *
 * Order matters and none of it is optional: SIGTERM first so daemons can
 * close their files, a bounded wait rather than an unbounded one (a service
 * that will not die must not hang the reboot forever), then sync twice and
 * remount /data read-only. The remount is the step that actually protects the
 * filesystem — sync alone leaves the journal open.
 */
static void do_shutdown(void)
{
    note("stage=shutdown\n");
    for (int i = 0; i < nsvc; i++)
        if (svcs[i].pid > 0)
            kill(svcs[i].pid, SIGTERM);
    for (int i = 0; i < 50; i++) {          /* up to 5s */
        if (waitpid(-1, NULL, WNOHANG) <= 0 && errno == ECHILD)
            break;
        usleep(100000);
    }
    kill(-1, SIGKILL);
    /* Spill the in-memory log to flash. One bounded write per shutdown is a
     * cost worth paying; the continuous stream that produced it is not. */
    char *dump[] = { "/system/bin/sh", "-c",
                     "cat /run/messages* > /data/emos/messages.last "
                     "2>/dev/null", NULL };
    run_wait(dump);
    sync();
    sync();
    mount(NULL, "/data", NULL, MS_REMOUNT | MS_RDONLY, NULL);
    note("stage=shutdown done\n");
    sync();
    reboot(RB_AUTOBOOT);
}

static void supervise(void)
{
    signal(SIGTERM, on_term);
    signal(SIGINT, on_term);

    for (;;) {
        if (want_shutdown)
            do_shutdown();

        /* Reap EVERYTHING, not just our own services: orphans reparent to PID
         * 1 and nobody else will ever collect them. */
        int st;
        pid_t d;
        while ((d = waitpid(-1, &st, WNOHANG)) > 0)
            for (int i = 0; i < nsvc; i++)
                if (svcs[i].pid == d) {
                    svcs[i].pid = -1;
                    if (time(NULL) - svcs[i].started < FAST_EXIT) {
                        if (++svcs[i].fails == 3)
                            note("svc %s failing fast, backing off\n",
                                 svcs[i].name);
                    } else
                        svcs[i].fails = 0;
                }

        time_t now = time(NULL);
        for (int i = 0; i < nsvc; i++) {
            struct svc *s = &svcs[i];
            if (s->pid > 0 || s->gone)
                continue;
            if (now - s->started < svc_backoff(s->fails))
                continue;
            /* Ordering, not just presence: a service with `after` waits for
             * it and keeps waiting, where a missing `req` means give up. */
            if (s->after && access(s->after, F_OK) != 0)
                continue;
            /* Not installed is not a failure to retry: EchoMuse may simply not
             * be on this device yet. Say so once and leave it alone. */
            const char *need = s->req ? s->req : (s->argv ? s->argv[0] : NULL);
            if (need && access(need, F_OK) != 0) {
                note("svc %s absent (%s)\n", s->name, need);
                s->gone = 1;
                continue;
            }
            s->started = now;
            if (!strcmp(s->name, "console"))
                s->pid = start_console();
            else if (!strcmp(s->name, "net")) {
                pid_t p = fork();
                if (p == 0) { net_main(); _exit(0); }
                s->pid = p;
            } else
                s->pid = spawn(s->argv);
        }
        sleep(1);
    }
}
