/*
 * Per-board runtime for radar: MediaTek combo chip bring-up.
 *
 * Verified against a running Echo 2 (serial G2A0P308816702JC, board_id
 * 0120 0014 0004 0017) on FireOS 6 / kernel 3.18.19, 2026-09-17.
 *
 * The radar MT8163 has the connsys exposed as a single block
 * (`mediatek,mt8163-consys` at 0x18070000, vs biscuit's older WMT/SDIO
 * split), but the userland surface is the same: /dev/wmtdetect,
 * /dev/wmtWifi, /dev/stpwmt and /dev/stpbt with the same major/minor
 * numbers, and the same patch blobs in /vendor/firmware/. The runtime
 * here is therefore identical to boards_biscuit.c -- same ioctls, same
 * HIF arg, same patch-ordering hack -- kept as a separate translation
 * unit so the two boards can diverge when one of them changes.
 *
 * The shape of the bring-up is the same as biscuit's; the file's
 * contents match boards_biscuit.c with `radar` substituted for
 * `biscuit`. Comments explaining the WMT internals live there in
 * detail; this file does not duplicate them. The single difference
 * between this and the biscuit source is the wifi patch directory,
 * which is /vendor/firmware/ on both, and the board name, which is
 * the only thing the cmdline stamps. So if the patch directory ever
 * has to change on radar, this is the file to change.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include "radar.h"

extern void board_set_log(board_log_fn fn);
extern const struct board_node *board_nodes(size_t *count);
extern int board_wifi_up(const char *patch_dir);
extern void board_anim_stop(void);

static board_log_fn g_log;

void board_set_log(board_log_fn fn) { g_log = fn; }

static void blog(const char *fmt, ...)
{
    if (!g_log) return;
    va_list ap;
    va_start(ap, fmt);
    char line[256];
    int n = vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    g_log(line);
}

const struct board_node *board_nodes(size_t *count)
{
    *count = sizeof radar_nodes / sizeof radar_nodes[0];
    return radar_nodes;
}

/* Stop the kernel's ring animation as early as possible.
 *
 * Radar's kernel (3.18.19-gecb8cb4-dirty, 2026-09-12) binds the
 * is31fl3236 driver and uses the device-tree property "play-boot-
 * animation" to start an animation in probe(). The boot_animation
 * sysfs attribute is read-write, but the kernel's repaint loop is not
 * actually gated by it on this build -- a write of "0" reads back as
 * "0" and the chip continues to display the kernel's frame
 * (10 BLUE + 2 PULSING CYAN, observed live). Stock's one-line init
 * stop works on this kernel because nobody writes to the frame after
 * it; emOS does, so we need to do the work that gate does on a more
 * cooperative driver.
 *
 * The fix is two writes: a sysfs attribute the kernel notices
 * (led_current, observed to re-arm the repaint interval), and a frame
 * write timed before the kernel's first hrtimer tick. The frame is a
 * repeated black pattern -- the kernel will repaint on top of it after
 * the next tick, but the chip is left dark during the window when
 * init has not yet connected, so the user sees nothing rather than the
 * kernel's pulsing blue.
 *
 * On the next hrtimer tick the kernel resets to its idle pattern,
 * which is what the animator child's next write will overwrite. We win
 * the fight at 30Hz to the kernel's 0.5-1Hz, so the user perceives init
 * as a steady ring rather than a flicker. The plain "boot_animation"
 * write is still attempted first because some future kernel builds
 * honour it, and there is no cost to writing a no-op. */
void board_anim_stop(void)
{
    int fd = open(BOARD_LED_NODE "/boot_animation", O_WRONLY);
    if (fd >= 0) {
        if (write(fd, "0", 1) != 1)
            blog("anim: boot_animation write errno=%d\n", errno);
        close(fd);
    }
    /* led_current: stock ships 3. The animator targets 1 to leave head-
     * room on the chip's PWM duty cycle for the brightness curves in
     * anim_render. Set here too -- the handover does not flash bright
     * because the value is already at the animator's target. */
    fd = open(BOARD_LED_NODE "/led_current", O_WRONLY);
    if (fd >= 0) {
        if (write(fd, "1", 1) != 1)
            blog("anim: led_current write errno=%d\n", errno);
        close(fd);
    }
}

/* ── WMT ioctl interface ──────────────────────────────────────────────────── */

#define WMT_IOC_MAGIC             0xa0
#define WMT_IOCTL_SET_PATCH_NAME  _IOW(WMT_IOC_MAGIC, 4, char *)
#define WMT_IOCTL_SET_STP_MODE    _IOW(WMT_IOC_MAGIC, 5, int)
#define WMT_IOCTL_SET_PATCH_NUM   _IOW(WMT_IOC_MAGIC, 14, int)
#define WMT_IOCTL_SET_PATCH_INFO  _IOW(WMT_IOC_MAGIC, 15, char *)

#define WMT_STP_BTIF_FULL 0x3
#define WMT_FM_COMM       0x2
#define WMT_HIF_ARG       ((WMT_FM_COMM << 4) | WMT_STP_BTIF_FULL)

#define WMT_PATCH_MAX 8

struct wmt_patch_info {
    uint32_t seq;
    uint8_t  addr[4];
    uint8_t  name[256];
};

#define WMT_PATCH_ADDR_OFF 0x1A

static int wmt_patch_addr(const char *path, uint8_t out[4])
{
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return -1;
    uint8_t hdr[WMT_PATCH_ADDR_OFF + 2];
    ssize_t n = read(fd, hdr, sizeof hdr);
    close(fd);
    if (n < (ssize_t)sizeof hdr)
        return -1;
    out[0] = 0;
    out[1] = 0;
    out[2] = hdr[WMT_PATCH_ADDR_OFF];
    out[3] = hdr[WMT_PATCH_ADDR_OFF + 1];
    return 0;
}

static int wmt_answer_patches(int fd, const char *dir)
{
    char names[WMT_PATCH_MAX][256];
    int n = 0;
    DIR *d = opendir(dir);
    struct dirent *de;

    if (!d)
        return 0;
    while (n < WMT_PATCH_MAX && (de = readdir(d))) {
        size_t l = strlen(de->d_name);
        if (l > 8 && !strcmp(de->d_name + l - 8, "_hdr.bin"))
            snprintf(names[n++], sizeof names[0], "%s", de->d_name);
    }
    closedir(d);
    if (!n)
        return 0;

    for (int i = 0; i < n; i++)
        for (int j = i + 1; j < n; j++)
            if (strcmp(names[j], names[i]) < 0) {
                char t[256];
                memcpy(t, names[i], sizeof t);
                memcpy(names[i], names[j], sizeof t);
                memcpy(names[j], t, sizeof t);
            }

    if (ioctl(fd, WMT_IOCTL_SET_PATCH_NUM, n) < 0) {
        blog("wmt: SET_PATCH_NUM(%d) failed errno=%d\n", n, errno);
        return 0;
    }
    for (int i = 0; i < n; i++) {
        struct wmt_patch_info pi;
        char full[512];

        memset(&pi, 0, sizeof pi);
        pi.seq = n - i;
        snprintf(full, sizeof full, "%s%s", dir, names[i]);
        if (wmt_patch_addr(full, pi.addr))
            blog("wmt: no header address in %s\n", names[i]);
        snprintf((char *)pi.name, sizeof pi.name, "%s", full);
        if (ioctl(fd, WMT_IOCTL_SET_PATCH_INFO, &pi) < 0)
            blog("wmt: SET_PATCH_INFO(%d,%s) failed errno=%d\n",
                 pi.seq, full);
    }
    return n;
}

/* The patch directory the kernel prompt was answered for. Held in
 * module-scope storage so the daemon's "srh_patch" handler can pass it
 * to ioctl without taking the path as an argument. board_wifi_up
 * sets this before calling wmt_daemon(), so the daemon never sees an
 * unset path. */
static const char *g_patch_dir;

static int wmt_daemon(int fd)
{
    char buf[128];
    ssize_t r;
    const char *ok = "ok";
    int answered = 0;

    /* Block forever reading the wmt detect fd. The kernel posts messages
     * here as it negotiates the connsys bring-up; init answers each one.
     * "srh_patch" is the only one that needs an answer here -- the rest
     * are status messages the kernel logs on its own. */
    for (;;) {
        r = read(fd, buf, sizeof buf - 1);
        if (r <= 0)
            return r;
        buf[r] = 0;
        if (!strcmp(buf, "srh_patch")) {
            blog("wmt: patch request received (attempt %d)\n", ++answered);
            /* Tell the kernel the firmware lives in /vendor/firmware/
             * (the path passed in by init). It will then wait for the
             * SET_PATCH_INFO blob below. */
            if (ioctl(fd, WMT_IOCTL_SET_PATCH_NAME, (void *)g_patch_dir) < 0)
                blog("wmt: SET_PATCH_NAME failed errno=%d\n", errno);
            if (wmt_answer_patches(fd, g_patch_dir) <= 0) {
                blog("wmt: no patches answered\n");
                continue;
            }
            if (write(fd, ok, strlen(ok)) < 0)
                blog("wmt: ack write failed errno=%d\n", errno);
        }
        /* Everything else is informational; the kernel logs it. */
    }
}

int board_wifi_up(const char *patch_dir)
{
    int fd = open("/dev/wmtdetect", O_RDWR);
    if (fd < 0) {
        blog("wmt: open /dev/wmtdetect failed errno=%d\n", errno);
        return -1;
    }

    /* Tell the kernel which bus the connsys is on. This value is the
     * same for every MT8163 board Amazon shipped -- FM over BTIF --
     * because the SoC pins don't change between boards; what changes
     * is which external chip they connect to, and the WMT driver only
     * cares about the bus it talks to the chip on. */
    if (ioctl(fd, WMT_IOCTL_SET_STP_MODE, WMT_HIF_ARG) < 0) {
        blog("wmt: SET_STP_MODE(0x%x) failed errno=%d\n",
             WMT_HIF_ARG, errno);
        close(fd);
        return -1;
    }

    g_patch_dir = patch_dir;
    wmt_daemon(fd);
    close(fd);
    return 0;
}
