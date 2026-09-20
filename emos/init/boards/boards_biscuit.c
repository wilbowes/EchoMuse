/*
 * Per-board runtime for biscuit: MediaTek combo chip bring-up.
 *
 * Everything here came from MediaTek's GPL source (the conn_soc variant,
 * which is what this kernel is built from) and every number below was
 * checked against the running driver on hardware, 2026-09-12.
 *
 * Two steps, and the second is the whole reason Amazon ships a launcher:
 *
 *   1. SET_PATCH_NAME then SET_STP_MODE. The SET_STP_MODE handler calls
 *      wmt_lib_set_hif() and posts WMT_OPID_HIF_CONF -- the "WMT HIF info
 *      added" line. Its argument is (fm << 4) | stp. A value it does not
 *      recognise is rejected by wmt_lib_set_hif with no hardware touched,
 *      so getting it wrong fails safe.
 *
 *   2. A daemon loop. Powering the chip makes the driver ask USERSPACE to
 *      locate the firmware patches: it posts the string "srh_patch" and
 *      blocks. The answer is SET_PATCH_NUM, then one SET_PATCH_INFO per
 *      patch, then "ok" written back to release it. The driver does not
 *      care who answers -- there is no registration of any kind -- so
 *      init answers. Without this, power-on dies at "patch info perpare
 *      fail" and there is no wlan0.
 *
 * On FireOS 6 the Amazon-launched equivalents do not work in emOS's
 * environment: wmt_loader exits 255, wmt_launcher runs but sits silent,
 * WMT_OPID_HIF_CONF is never posted, the chip never powers on, and the
 * /dev/wmtWifi write returns EIO. They coordinate through Android
 * properties, and emOS has no property service -- but building one to
 * satisfy them would make Amazon's userspace MORE load-bearing, which
 * is the wrong direction. So this file talks to the kernel driver
 * itself.
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

/* boards_biscuit.c does NOT include boards.h -- see that header's
 * comment for why. The three prototypes below, plus the types from
 * biscuit.h, are everything this TU needs from the contract. */
#include "biscuit.h"

extern void board_set_log(board_log_fn fn);
extern const struct board_node *board_nodes(size_t *count);
extern int board_wifi_up(const char *patch_dir);
extern void board_anim_stop(void);

/* The board_log_fn sink set by init.c. NULL until set, and the log
 * calls below all check -- a board built with a misconfigured link
 * still compiles, just without diagnostic output. */
static board_log_fn g_log;

void board_set_log(board_log_fn fn) { g_log = fn; }

static void blog(const char *fmt, ...)
{
    if (!g_log) return;
    va_list ap;
    va_start(ap, fmt);
    /* netlog_line() in init.c takes one pre-formatted string; a varargs
     * function pointer cannot forward a va_list portably, so we format
     * here and pass a single argument. Buffer size matches init.c's
     * note()/netlog() buffers so a long line truncates the same way. */
    char line[256];
    int n = vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    g_log(line);
}

const struct board_node *board_nodes(size_t *count)
{
    *count = sizeof biscuit_nodes / sizeof biscuit_nodes[0];
    return biscuit_nodes;
}

/* Tell the is31fl3236 driver to drop its ring animation now, so it
 * stops repainting frames between the moment init runs and the moment
 * the animator child claims the ring.
 *
 * The kernel's animation is set up at probe time and runs until userspace
 * writes "0" to the boot_animation sysfs attribute, exactly the way
 * Stock Amazon's init.recovery.leds.rc does it on the first boot of a
 * stock device. We do the same one line, but here, in C, because the
 * initrc parser is an init-stage we have skipped.
 *
 * Returns silently on failure. The write returns ENOENT on a build where
 * the device tree node was not built (a kconfig or DT omission), and
 * ignoring that is right -- there is no animation to stop on such a
 * device, so init's animator child stays the sole writer and never has
 * to win a fight against anything.
 *
 * Idempotent: a second call writes 0 to the same attribute and costs
 * nothing. init.c calls this exactly once, from main() before the
 * animator child is forked.
 *
 * It also writes "0" to led_current here: stock ships 3, which is the
 * value the firmware's idle pattern was tuned for, but init's animator
 * targets 1 (per-frame current) to stay well below the chip's maximum.
 * Lowering it at boot means the handover does not flash bright. */
void board_anim_stop(void)
{
    int fd = open(BOARD_LED_NODE "/boot_animation", O_WRONLY);
    if (fd >= 0) {
        if (write(fd, "0", 1) != 1)
            blog("anim: boot_animation write errno=%d\n", errno);
        close(fd);
    }
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

/* wmt_dev.h: STP_UART_FULL 1, STP_UART_MAND 2, STP_BTIF_FULL 3, STP_SDIO 4.
 * wmt_core.h: WMT_FM_I2C 1, WMT_FM_COMM 2.
 * biscuit is BTIF -- the driver reports back "hifType 2" for this value. */
#define WMT_STP_BTIF_FULL 0x3
#define WMT_FM_COMM       0x2
#define WMT_HIF_ARG       ((WMT_FM_COMM << 4) | WMT_STP_BTIF_FULL)

#define WMT_PATCH_MAX 8

/* WMT_PATCH_INFO, wmt_lib.h. The layout is fixed by the driver's
 * copy_from_user, so the field order and the 256-byte name are not ours
 * to choose. */
struct wmt_patch_info {
    uint32_t seq;
    uint8_t  addr[4];
    uint8_t  name[256];
};

/* The four address bytes the driver splices into WMT_PATCH_P_ADDRESS_CMD.
 *
 * Taken from Amazon's own wmt_launcher, observed live under an LD_PRELOAD
 * ioctl shim on a rooted FireOS 6 (2026-09-12) rather than guessed: it
 * sends 00 00 06 00 for ROMv2_lm_patch_1_0_hdr.bin and 00 00 0e f0 for
 * ROMv2_lm_patch_1_1_hdr.bin. The two live bytes are at header offset
 * 0x1A and the top two are ZERO -- 0x18 is the tail of ucPLat in the
 * 28-byte WMT_PATCH header (ucDateTime[16], u2HwVer, u2SwVer, u4PatchVer,
 * ucPLat[4]), and sending all four from 0x18 puts rubbish in the high
 * half. */
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

/* Answer one "srh_patch". Returns the number of patches reported. */
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
        /* The ROM patches are the *_hdr.bin files; WIFI_RAM_CODE_* and
         * the .cfg in the same directory are not patches and must not
         * be counted, or the driver waits for a download that never
         * comes. */
        if (l > 8 && !strcmp(de->d_name + l - 8, "_hdr.bin"))
            snprintf(names[n++], sizeof names[0], "%s", de->d_name);
    }
    closedir(d);
    if (!n)
        return 0;

    /* Download order is the driver's `dowloadSeq`, 1-based. The files
     * sort into it by name (…_1_0_hdr, …_1_1_hdr), so sort rather than
     * trust readdir, whose order is the filesystem's and not stable. */
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
        /* Download order runs BACKWARDS through the sorted names:
         * Amazon's launcher gives ROMv2_lm_patch_1_0 seq 2 and ..._1_1
         * seq 1, so the higher-numbered file is downloaded first.
         * Observed live; assigning 1,2 in name order sends them in the
         * wrong order. */
        pi.seq = n - i;
        snprintf(full, sizeof full, "%s%s", dir, names[i]);
        if (wmt_patch_addr(full, pi.addr))
            blog("wmt: no header address in %s\n", names[i]);
        /* FULL PATH, not a bare name. wmt_dev_patch_get does not use
         * request_firmware -- it filp_open()s this string exactly as
         * given, from kernel context, so a bare name is opened
         * relative to / and fails with "load file (…) fail, iRet(-1)".
         * SET_PATCH_NAME does not get prepended for us. */
        snprintf((char *)pi.name, sizeof pi.name, "%s", full);
        if (ioctl(fd, WMT_IOCTL_SET_PATCH_INFO, &pi) < 0)
            blog("wmt: SET_PATCH_INFO(%d,%s) failed errno=%d\n",
                 pi.seq, names[i], errno);
    }
    return n;
}

/* Stand in for wmt_launcher for as long as the chip is up.
 *
 * Never returns. The driver blocks its power-on inside wmt_ctrl_ul_cmd
 * until this answers, so the loop has to outlive the bring-up rather
 * than run once: a chip reset asks again. */
static void wmt_daemon(int fd, const char *dir)
{
    for (;;) {
        struct pollfd pfd = { .fd = fd, .events = POLLIN };
        if (poll(&pfd, 1, -1) < 0) {
            if (errno == EINTR)
                continue;
            blog("wmt: poll failed errno=%d\n", errno);
            return;
        }
        char cmd[64] = { 0 };
        ssize_t n = read(fd, cmd, sizeof cmd - 1);
        if (n <= 0)
            continue;
        cmd[n] = '\0';
        if (!strncmp(cmd, "srh_patch", 9)) {
            int got = wmt_answer_patches(fd, dir);
            blog("wmt: srh_patch -> %d patch(es)\n", got);
            /* Anything but "ok" is read as failure by the driver, so
             * say ok only when we actually found something. */
            if (write(fd, got ? "ok" : "fail", got ? 2 : 4) < 0)
                blog("wmt: reply failed errno=%d\n", errno);
        } else {
            blog("wmt: unhandled daemon cmd '%s'\n", cmd);
            if (write(fd, "fail", 4) < 0)
                blog("wmt: reply failed errno=%d\n", errno);
        }
    }
}

int board_wifi_up(const char *patch_dir)
{
    int fd = open("/dev/stpwmt", O_RDWR);
    if (fd < 0) {
        blog("wmt: open /dev/stpwmt failed errno=%d\n", errno);
        return -1;
    }
    if (ioctl(fd, WMT_IOCTL_SET_PATCH_NAME, patch_dir) < 0)
        blog("wmt: SET_PATCH_NAME failed errno=%d\n", errno);

    int r = ioctl(fd, WMT_IOCTL_SET_STP_MODE, WMT_HIF_ARG);
    blog("wmt: SET_STP_MODE(0x%x) rc=%d errno=%d\n",
         WMT_HIF_ARG, r, r ? errno : 0);
    if (r < 0) {
        close(fd);
        return -1;
    }

    pid_t p = fork();
    if (p == 0) {
        wmt_daemon(fd, patch_dir);
        _exit(0);
    }
    /* The parent keeps its own copy closed: the daemon owns the fd,
     * and the driver's command state is per-open. */
    close(fd);
    return p > 0 ? 0 : -1;
}
