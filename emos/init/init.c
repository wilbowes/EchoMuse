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
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/wait.h>
#include <termios.h>
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

static void note(const char *fmt, ...)
{
    char line[256];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    if (n < 0)
        return;
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
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    if (n <= 0)
        return;
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
    r = ifup("wlan0");
    netlog("wlan0 present=%d ifup=%d\n",
         access("/sys/class/net/wlan0", F_OK) == 0, r);

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
    pid_t wpa = spawn(supp);
    pid_t dhc = -1;
    int nudges = 0, dry = 0;

    for (;;) {
        pid_t d;
        while ((d = waitpid(-1, &st, WNOHANG)) > 0) {
            if (d == launcher)   launcher = spawn(launch);
            else if (d == wpa)   wpa = spawn(supp);
            else if (d == dhc)   dhc = -1;
        }

        if (readint("/sys/class/net/wlan0/carrier") != 1) {
            if (dhc > 0)
                kill(dhc, SIGTERM);
            if (nudges++ % 3 == 0)
                spawn(reassoc);
            dry = 0;
        } else {
            nudges = 0;
            if (dhc < 0) {
                dhc = spawn(dhcp);
                dry = 0;
            } else if (has_ip("wlan0")) {
                dry = 0;
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

int main(void)
{
    char buf[512];

    /* mknod's mode is masked by the umask, so without this every node below
     * comes out 0644 no matter what it asks for — which is how dhcpcd's hook
     * script ended up unable to open /dev/null for writing. */
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

    snprintf(buf, sizeof buf, "EM64-INIT-OK acm-console\n");
    write_at(0, buf, strlen(buf));

    int fd = open("/proc/version", O_RDONLY);
    if (fd >= 0) {
        int n = read(fd, buf, sizeof buf - 1);
        close(fd);
        if (n > 0) write_at(512, buf, n);
    }
    note("stage=mounts done devtmpfs_rc=%d tty=%d\n", dtr, access(TTY, F_OK));

    /* /system read-only: this is a diagnostic boot and nothing here should be
     * able to damage the Android install we still rely on for recovery. */
    mkdir("/system", 0755);
    mknod("/dev/block/mmcblk0p13", S_IFBLK | 0600, makedev(179, 13));
    int r = mount("/dev/block/mmcblk0p13", "/system", "ext4", MS_RDONLY, NULL);
    note("stage=mount_system rc=%d errno=%d sh=%d\n", r, r ? errno : 0,
         access("/system/bin/sh", X_OK));

    /* /data read-WRITE: the firmware keeps config, wake-word models and logs
     * there. /system stays read-only — nothing here should be able to damage
     * the Android install we still rely on for recovery. */
    mkdir("/data", 0755);
    mknod("/dev/block/mmcblk0p16", S_IFBLK | 0600, makedev(179, 16));
    r = mount("/dev/block/mmcblk0p16", "/data", "ext4", 0, NULL);
    note("stage=mount_data rc=%d errno=%d\n", r, r ? errno : 0);

    /* Android's own root has these two symlinks and a surprising amount of
     * /system depends on them — the kernel firmware loader most of all, which
     * is what kept wlan0 from ever appearing. */
    symlink("/system/etc", "/etc");
    symlink("/system/vendor", "/vendor");

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

    for (int i = 0; i < 15; i++) {
        sleep(1);
        char tag[32];
        snprintf(tag, sizeof tag, "wait=%d tty=%d", i, access(TTY, F_OK));
        rdstate(tag);
        if (access(TTY, F_OK) == 0)
            break;
    }

    /* WiFi runs in its own process: the wmtWifi write blocks for ~13s and the
     * daemons behind it need supervising, neither of which should hold up the
     * console. */
    pid_t net = fork();
    if (net == 0) {
        net_main();
        _exit(0);
    }
    netlog("stage started pid=%d\n", (int)net);

    /* A console that exits once because the host had not attached yet is
     * indistinguishable from one that never worked, so respawn. */
    for (int gen = 0;; gen++) {
        int t = open(TTY, O_RDWR | O_NOCTTY);
        if (t < 0) {
            if (gen % 30 == 0)
                note("open %s failed errno=%d\n", TTY, errno);
            sleep(2);
            continue;
        }
        pid_t pid = fork();
        if (pid == 0) {
            setsid();
            ioctl(t, TIOCSCTTY, 1);
            dup2(t, 0); dup2(t, 1); dup2(t, 2);
            if (t > 2) close(t);
            char *argv[] = { "/system/bin/sh", NULL };
            char *envp[] = { "HOME=/", "TERM=vt100",
                             "PATH=/sbin:/system/bin:/system/xbin", NULL };
            execve("/system/bin/sh", argv, envp);
            _exit(127);
        }
        close(t);
        if (gen < 3)
            note("shell started gen=%d pid=%d\n", gen, (int)pid);
        int st = 0;
        waitpid(pid, &st, 0);
        if (gen < 3)
            note("shell exited gen=%d status=%d\n", gen, st);
        sleep(1);
    }
}
