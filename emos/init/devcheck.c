/* Prove init.c makes /dev the way the kernel describes it.
 *
 * Every node emOS opens comes from mknodes(): it reads each device's uevent
 * under /sys/dev and makes the node it names. Getting that wrong is the quiet
 * kind of wrong. A node with the wrong numbers opens SOMETHING, and the
 * console's shell then sits on it saying nothing. That is what the table this
 * replaced did on the Echo Dot 3, where 247:0 is the jack detector rather than
 * ttyGS0 (41C8, 2026-10-24).
 *
 * The uevent text below was read off real kernels: biscuit's 3.18 (VVV) and
 * the Dot 3's 4.4 (41C8). The rest are the edges of what a uevent can say,
 * and the names init must refuse because they would climb out of /dev.
 *
 * init.c is included whole, as the other checks do, and mknod() is redirected
 * so the walk runs without root.
 *
 *   cc -O2 -o devcheck devcheck.c && ./devcheck
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static char sysroot[64], devroot[64];

#define LEDDIR  "/tmp/emos-devcheck"
#define SYSDEV  sysroot
#define DEVROOT devroot

/* Every mknod init makes lands here: a plain file, plus a record of what it
 * would have been. */
struct made { char path[160]; mode_t mode; dev_t dev; };
static struct made made[32];
static int nmade;

static int fake_mknod(const char *path, mode_t mode, dev_t dev)
{
    if (access(path, F_OK) == 0) {
        errno = EEXIST;
        return -1;
    }
    FILE *f = fopen(path, "w");
    if (!f)
        return -1;
    fclose(f);
    if (nmade < 32) {
        snprintf(made[nmade].path, sizeof made[nmade].path, "%s", path);
        made[nmade].mode = mode;
        made[nmade].dev = dev;
        nmade++;
    }
    return 0;
}

#define mknod fake_mknod
#define main  init_main_unused

#include "init.c"

#undef main
#undef mknod

static int fails;

static void check(const char *what, const char *ev, int block, const char *want,
                  unsigned wmaj, unsigned wmin, mode_t wmode)
{
    char path[160] = "";
    unsigned ma = 0, mi = 0;
    mode_t mode = 0;
    int r = uevent_node(ev, block, path, sizeof path, &ma, &mi, &mode);
    char exp[160] = "";
    if (want)
        snprintf(exp, sizeof exp, "%s/%s", devroot, want);
    int ok = want ? (r == 0 && !strcmp(path, exp) && ma == wmaj &&
                     mi == wmin && mode == wmode)
                  : r != 0;
    if (ok) {
        printf("ok    %-44s -> %s\n", what, want ? want : "refused");
    } else {
        printf("FAIL  %-44s -> r=%d %s %u:%u %o, wanted %s %u:%u %o\n", what,
               r, path, ma, mi, mode, want ? exp : "refused", wmaj, wmin,
               wmode);
        fails++;
    }
}

static void put(const char *kind, const char *id, const char *ev)
{
    char p[200];
    snprintf(p, sizeof p, "%s/%s/%s", sysroot, kind, id);
    mkdir(p, 0755);
    strcat(p, "/uevent");
    FILE *f = fopen(p, "w");
    fputs(ev, f);
    fclose(f);
}

static const struct made *find(const char *rel)
{
    char p[160];
    snprintf(p, sizeof p, "%s/%s", devroot, rel);
    for (int i = 0; i < nmade; i++)
        if (!strcmp(made[i].path, p))
            return &made[i];
    return NULL;
}

static void expect(const char *rel, mode_t type, unsigned ma, unsigned mi,
                   mode_t perm)
{
    const struct made *m = find(rel);
    if (m && (m->mode & S_IFMT) == type && major(m->dev) == ma &&
        minor(m->dev) == mi && (m->mode & 07777) == perm) {
        printf("ok    walk made %-34s %u:%u %o\n", rel, ma, mi, perm);
    } else {
        printf("FAIL  walk made %-34s wanted %u:%u %o\n", rel, ma, mi, perm);
        fails++;
    }
}

int main(void)
{
    snprintf(sysroot, sizeof sysroot, "/tmp/emos-devcheck-sys.%d", getpid());
    snprintf(devroot, sizeof devroot, "/tmp/emos-devcheck-dev.%d", getpid());

    /* Off the Dot 3 (41C8). ttyGS0 is the node the old table got wrong. */
    check("donut ttyGS0", "MAJOR=246\nMINOR=0\nDEVNAME=ttyGS0\n", 0,
          "ttyGS0", 246, 0, 0600);
    check("donut /dev/null, DEVMODE", "MAJOR=1\nMINOR=3\nDEVNAME=null\n"
          "DEVMODE=0666\n", 0, "null", 1, 3, 0666);
    check("donut ptmx, DEVMODE", "MAJOR=5\nMINOR=2\nDEVNAME=ptmx\n"
          "DEVMODE=0666\n", 0, "ptmx", 5, 2, 0666);
    check("donut console, no DEVMODE", "MAJOR=5\nMINOR=1\nDEVNAME=console\n",
          0, "console", 5, 1, 0600);
    check("donut pcm, in a subdirectory", "MAJOR=116\nMINOR=3\n"
          "DEVNAME=snd/pcmC0D0p\n", 0, "snd/pcmC0D0p", 116, 3, 0600);
    check("donut input event", "MAJOR=13\nMINOR=67\nDEVNAME=input/event3\n",
          0, "input/event3", 13, 67, 0600);
    check("donut usb, three levels deep", "MAJOR=189\nMINOR=0\n"
          "DEVNAME=bus/usb/001/001\nDEVTYPE=usb_device\n", 0,
          "bus/usb/001/001", 189, 0, 0600);
    check("donut partition", "MAJOR=179\nMINOR=10\nDEVNAME=mmcblk0p10\n"
          "DEVTYPE=partition\nPARTN=10\nPARTNAME=boot_a\n", 1,
          "block/mmcblk0p10", 179, 10, 0600);
    check("donut whole disk", "MAJOR=179\nMINOR=0\nDEVNAME=mmcblk0\n"
          "DEVTYPE=disk\nNPARTS=16\n", 1, "block/mmcblk0", 179, 0, 0600);

    /* Off biscuit (VVV), where the old table was right. */
    check("biscuit ttyGS0", "MAJOR=247\nMINOR=0\nDEVNAME=ttyGS0\n", 0,
          "ttyGS0", 247, 0, 0600);
    check("biscuit stpwmt", "MAJOR=190\nMINOR=0\nDEVNAME=stpwmt\n", 0,
          "stpwmt", 190, 0, 0600);

    /* What a uevent may leave out, or put in another order. */
    check("fields in another order", "DEVNAME=zero\nDEVMODE=0666\nMINOR=5\n"
          "MAJOR=1\n", 0, "zero", 1, 5, 0666);
    check("no trailing newline", "MAJOR=1\nMINOR=8\nDEVNAME=random", 0,
          "random", 1, 8, 0600);
    check("minor above 255", "MAJOR=13\nMINOR=1024\nDEVNAME=input/event9\n",
          0, "input/event9", 13, 1024, 0600);
    check("no DEVNAME: a device with no node", "MAJOR=4\nMINOR=64\n", 0,
          NULL, 0, 0, 0);
    check("empty DEVNAME", "MAJOR=4\nMINOR=64\nDEVNAME=\n", 0, NULL, 0, 0, 0);
    check("no MAJOR", "MINOR=3\nDEVNAME=null\n", 0, NULL, 0, 0, 0);
    check("no MINOR", "MAJOR=1\nDEVNAME=null\n", 0, NULL, 0, 0, 0);
    check("empty file", "", 0, NULL, 0, 0, 0);
    check("DEVMODE past 0777 is ignored", "MAJOR=1\nMINOR=3\nDEVNAME=null\n"
          "DEVMODE=4777\n", 0, "null", 1, 3, 0600);

    /* Names that would put a node outside /dev. */
    check("absolute name", "MAJOR=1\nMINOR=3\nDEVNAME=/etc/passwd\n", 0,
          NULL, 0, 0, 0);
    check("leading ..", "MAJOR=1\nMINOR=3\nDEVNAME=../etc/passwd\n", 0,
          NULL, 0, 0, 0);
    check("embedded ..", "MAJOR=1\nMINOR=3\nDEVNAME=snd/../../x\n", 0,
          NULL, 0, 0, 0);
    check("trailing ..", "MAJOR=1\nMINOR=3\nDEVNAME=snd/..\n", 0,
          NULL, 0, 0, 0);
    check("a name that only starts with ..", "MAJOR=1\nMINOR=3\n"
          "DEVNAME=..x\n", 0, "..x", 1, 3, 0600);
    check("a name with .. inside a component", "MAJOR=1\nMINOR=3\n"
          "DEVNAME=a..b\n", 0, "a..b", 1, 3, 0600);
    check("space in the name", "MAJOR=1\nMINOR=3\nDEVNAME=a b\n", 0,
          NULL, 0, 0, 0);
    check("control character in the name", "MAJOR=1\nMINOR=3\n"
          "DEVNAME=a\tb\n", 0, NULL, 0, 0, 0);
    char longev[400];
    snprintf(longev, sizeof longev, "MAJOR=1\nMINOR=3\nDEVNAME=%0300d\n", 0);
    check("name too long for the path", longev, 0, NULL, 0, 0, 0);

    /* The whole walk, over a fake /sys/dev. */
    char p[200];
    mkdir(sysroot, 0755);
    snprintf(p, sizeof p, "%s/char", sysroot);
    mkdir(p, 0755);
    snprintf(p, sizeof p, "%s/block", sysroot);
    mkdir(p, 0755);
    mkdir(devroot, 0755);
    snprintf(p, sizeof p, "%s/block", devroot);
    mkdir(p, 0755);
    put("char", "246:0", "MAJOR=246\nMINOR=0\nDEVNAME=ttyGS0\n");
    put("char", "1:3", "MAJOR=1\nMINOR=3\nDEVNAME=null\nDEVMODE=0666\n");
    put("char", "116:3", "MAJOR=116\nMINOR=3\nDEVNAME=snd/pcmC0D0p\n");
    put("char", "4:64", "MAJOR=4\nMINOR=64\n");
    put("block", "179:15", "MAJOR=179\nMINOR=15\nDEVNAME=mmcblk0p15\n"
        "DEVTYPE=partition\nPARTN=15\nPARTNAME=cache\n");
    int n = mknodes();
    expect("ttyGS0", S_IFCHR, 246, 0, 0600);
    expect("null", S_IFCHR, 1, 3, 0666);
    expect("snd/pcmC0D0p", S_IFCHR, 116, 3, 0600);
    expect("block/mmcblk0p15", S_IFBLK, 179, 15, 0600);
    if (n == 4) {
        printf("ok    first walk made 4 nodes\n");
    } else {
        printf("FAIL  first walk made %d nodes, wanted 4\n", n);
        fails++;
    }
    /* A device registered later appears on the next walk, and the ones
     * already made are left alone. */
    put("char", "190:0", "MAJOR=190\nMINOR=0\nDEVNAME=stpwmt\n");
    n = mknodes();
    expect("stpwmt", S_IFCHR, 190, 0, 0600);
    if (n == 1) {
        printf("ok    second walk made only the new node\n");
    } else {
        printf("FAIL  second walk made %d nodes, wanted 1\n", n);
        fails++;
    }
    if (umask(022) == 022) {
        printf("ok    walk put the umask back\n");
    } else {
        printf("FAIL  walk left the umask changed\n");
        fails++;
    }

    char rm[200];
    snprintf(rm, sizeof rm, "rm -rf %s %s", sysroot, devroot);
    if (system(rm) != 0)
        printf("note  could not remove the scratch trees\n");

    printf(fails ? "\n%d FAILED\n" : "\nall passed\n", fails);
    return fails != 0;
}
