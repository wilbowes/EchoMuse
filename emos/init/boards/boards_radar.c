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
extern int board_wifi_prepare(void);
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
#define RADAR_IOCTL_SET_CHIP_ID   _IOW('w', 1, int)
#define WMT_IOCTL_SET_PATCH_NAME  _IOW(WMT_IOC_MAGIC, 4, char *)
#define WMT_IOCTL_SET_STP_MODE    _IOW(WMT_IOC_MAGIC, 5, int)
#define WMT_IOCTL_RADAR_SETUP_18  _IOW(WMT_IOC_MAGIC, 24, int)
#define WMT_IOCTL_RADAR_SETUP_0D  _IOW(WMT_IOC_MAGIC, 13, int)
#define WMT_IOCTL_RADAR_SETUP_07  _IOW(WMT_IOC_MAGIC, 7, int)
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

/* Stand in for the stock launcher's patch-answer loop, for as long as
 * the chip is up.
 *
 * Verified on radar 2026-09-18: the kernel posts "srh_patch" HERE, on
 * stpwmt (a background read caught the string on this node and nothing
 * on wmtdetect), each time mtk_wmtd's power-on cycle reaches the patch
 * stage. The answer goes back over the SAME fd: SET_PATCH_NUM, one
 * SET_PATCH_INFO per patch, then an "ok" write to release the waiting
 * kernel thread.
 *
 * The loop must survive short reads. stpwmt's read returns <= 0 while
 * the chip is between power cycles (mtk_wmtd backs its retry off to
 * ~48s after fast failures), and the first version of this daemon
 * exited on the first such read -- silently, with no log line, because
 * the exit path had no blog(). The kernel then logged "wait signal
 * timeout" -> "patch info perpare fail" on every cycle with nobody
 * listening. Biscuit's daemon survives this by design (poll, then
 * `continue` on n <= 0); this one now does the same. */
static int wmt_daemon(int fd)
{
    char buf[128];
    ssize_t r;
    int answered = 0;

    for (;;) {
        struct pollfd pfd = { .fd = fd, .events = POLLIN };
        int pr = poll(&pfd, 1, -1);
        if (pr < 0) {
            if (errno == EINTR)
                continue;
            blog("wmt: poll failed errno=%d\n", errno);
            return -1;
        }
        r = read(fd, buf, sizeof buf - 1);
        if (r <= 0) {
            /* Chip cycled or no data yet -- keep listening. */
            usleep(50000);
            continue;
        }
        buf[r] = 0;
        if (!strcmp(buf, "srh_patch")) {
            blog("wmt: patch request received (attempt %d)\n", ++answered);
            /* Re-assert the patch directory each time: the kernel's
             * patch search may not carry state across power cycles. */
            if (ioctl(fd, WMT_IOCTL_SET_PATCH_NAME, (void *)g_patch_dir) < 0)
                blog("wmt: SET_PATCH_NAME failed errno=%d\n", errno);
            int got = wmt_answer_patches(fd, g_patch_dir);
            if (got <= 0) {
                blog("wmt: no patches answered\n");
                /* Answer "fail" rather than silence: the kernel thread
                 * is blocked on a completion either way, and letting it
                 * time out just stretches each retry cycle out. */
                if (write(fd, "fail", 4) < 0)
                    blog("wmt: fail write errno=%d\n", errno);
                continue;
            }
            if (write(fd, "ok", 2) < 0)
                blog("wmt: ack write failed errno=%d\n", errno);
        } else {
            blog("wmt: unhandled daemon cmd '%s'\n", buf);
        }
    }
}

static int radar_loader_fallback(void)
{
    static const char *const loaders[] = {
        "/system/vendor/bin/wmt_loader",
        "/system/bin/wmt_loader",
        NULL,
    };

    for (int i = 0; loaders[i]; i++) {
        if (access(loaders[i], X_OK) != 0)
            continue;
        pid_t p = fork();
        if (p == 0) {
            execl(loaders[i], loaders[i], (char *)NULL);
            _exit(127);
        }
        if (p > 0) {
            blog("wmt: radar fallback launched %s\n", loaders[i]);
            return 0;
        }
        blog("wmt: fork(%s) failed errno=%d\n", loaders[i], errno);
    }

    blog("wmt: no radar wmt_loader available; wifi cannot come up\n");
    return -1;
}

int board_wifi_prepare(void)
{
    int detect = open("/dev/wmtdetect", O_RDWR);
    if (detect < 0) {
        blog("wmt: open /dev/wmtdetect failed errno=%d\n", errno);
        return -1;
    }
    int chip_id = 0x8163;
    int r = ioctl(detect, RADAR_IOCTL_SET_CHIP_ID, chip_id);
    blog("wmt: radar chip id 0x%x rc=%d errno=%d\n",
         chip_id, r, r ? errno : 0);
    close(detect);
    if (r < 0)
        return -1;

    return 0;
}

int board_wifi_up(const char *patch_dir)
{
    if (!patch_dir || !*patch_dir)
        patch_dir = "/system/vendor/firmware/";

    /* stpwmt carries the whole bring-up on this kernel: the HIF conf, the
     * setup ioctls AND the "srh_patch" request posted during power-on.
     * Verified on radar 2026-09-18 by a background read on both nodes:
     * "srh_patch" arrives here, never on wmtdetect.
     *
     * wmtdetect has one job -- the chip-id ioctl in board_wifi_prepare()
     * -- and nothing else; opening it for the daemon gets no data and
     * the patch search times out with nobody listening. */
    int fd = open("/dev/stpwmt", O_RDWR);
    if (fd < 0) {
        blog("wmt: open /dev/stpwmt failed errno=%d\n", errno);
        return -1;
    }

    g_patch_dir = patch_dir;
    pid_t p = fork();
    if (p == 0) {
        wmt_daemon(fd);
        _exit(0);
    }
    if (p < 0) {
        blog("wmt: fork patch daemon failed errno=%d\n", errno);
        close(fd);
        return -1;
    }

    int r;
    r = ioctl(fd, WMT_IOCTL_SET_STP_MODE, WMT_HIF_ARG);
    blog("wmt: SET_STP_MODE(0x%x) rc=%d errno=%d\n",
         WMT_HIF_ARG, r, r ? errno : 0);
    if (r < 0) {
        close(fd);
        return radar_loader_fallback();
    }

    /* Radar's launcher performs three setup ioctls after SET_STP_MODE. The
     * kernel accepts SET_STP_MODE on its own but leaves HIF unset; the next
     * /dev/wmtWifi write then fails with EIO. Values captured from the
     * stock radar launcher. 0x07 takes a POINTER in the kernel's
     * unlocked_ioctl -- it ran the whole power-on cycle and only then
     * failed copy-out with EFAULT when handed the raw value 1 (2.3s in
     * userspace, measured 2026-09-18) -- so pass the address of an int,
     * like the stock launcher does. */
    r = ioctl(fd, WMT_IOCTL_RADAR_SETUP_18, 0);
    blog("wmt: radar setup 0x18 rc=%d errno=%d\n", r, r ? errno : 0);
    r = ioctl(fd, WMT_IOCTL_RADAR_SETUP_0D, 0);
    blog("wmt: radar setup 0x0d rc=%d errno=%d\n", r, r ? errno : 0);
    int one = 1;
    r = ioctl(fd, WMT_IOCTL_RADAR_SETUP_07, &one);
    blog("wmt: radar setup 0x07 rc=%d errno=%d\n", r, r ? errno : 0);

    close(fd);
    return 0;
}
