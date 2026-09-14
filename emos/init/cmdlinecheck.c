/* Prove init.c reads `emos.system=` off the kernel cmdline the way the packer
 * writes it.
 *
 * This decides which partition becomes /system, and getting it wrong is the
 * quiet kind of wrong: every system partition on these devices holds a valid
 * FireOS, so a misparse does not fail the mount — it mounts a DIFFERENT
 * Amazon userspace than the one this image was built beside, boots, lights the
 * ring and looks perfectly healthy. The pairing is only visible afterwards, as
 * version skew between the kernel and bionic, which presents as anything at
 * all.
 *
 * It is also a parser over a string the OTHER half of the project writes — the
 * controller's packer stamps it — which is this project's stated criterion for
 * owing an off-target check.
 *
 * init.c is included whole, the same trick ringsim.c, pwcheck.c, tmoutcheck.c
 * and wpacheck.c use, so this drives the real function rather than a copy.
 *
 *   cc -O2 -o cmdlinecheck cmdlinecheck.c && ./cmdlinecheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LEDDIR "/tmp/emos-cmdlinecheck"
#define main   init_main_unused

#include "init.c"

#undef main

static int fails;

static void check(const char *what, const char *cmdline, int want)
{
    int got = cmdline_system_part(cmdline);
    if (got == want) {
        printf("ok    %-46s -> p%d\n", what, got);
    } else {
        printf("FAIL  %-46s -> p%d, wanted p%d\n", what, got, want);
        fails++;
    }
}

int main(void)
{
    const int D = SYSTEM_PART_DEFAULT;

    /* What the packer actually writes, in the position it writes it: last,
     * after the ramoops parameters it also appends. */
    check("stamped, at the end",
          "bootopt=64S3 ro init=/init ramoops.dump_oops=1 "
          "emos.system=/dev/block/mmcblk0p13", 13);
    check("stamped, the other slot's system",
          "bootopt=64S3 ro init=/init emos.system=/dev/block/mmcblk0p14", 14);
    check("stamped first",
          "emos.system=/dev/block/mmcblk0p14 bootopt=64S3 ro", 14);
    check("stamped in the middle",
          "ro emos.system=/dev/block/mmcblk0p14 init=/init", 14);
    check("trailing newline, as /proc/cmdline gives it",
          "ro emos.system=/dev/block/mmcblk0p14\n", 14);

    /* Absent is an image built before the stamp existed. It must keep booting
     * exactly as it did, which means p13 and not a refusal. */
    check("absent -- an older image", "bootopt=64S3 ro init=/init", D);
    check("empty cmdline", "", D);

    /* TOKEN BOUNDARY. A bare strstr() takes the value of a parameter that
     * merely ENDS in our key, and mounts whatever it names. */
    check("a longer key ending in ours",
          "ro xemos.system=/dev/block/mmcblk0p14", D);
    check("our key as a value of something else",
          "ro other=emos.system=/dev/block/mmcblk0p14", D);
    check("the key with no '='", "ro emos.system", D);
    check("the key with an empty value", "ro emos.system=", D);

    /* Anything we do not recognise falls back rather than being interpreted
     * generously -- a stamp from a builder we do not know is not a licence to
     * guess at a partition. */
    check("a bare number", "ro emos.system=13", D);
    check("some other device", "ro emos.system=/dev/sda1", D);
    check("a different eMMC", "ro emos.system=/dev/block/mmcblk1p13", D);
    check("a path with a suffix", "ro emos.system=/dev/block/mmcblk0p13x", D);
    check("non-numeric partition", "ro emos.system=/dev/block/mmcblk0pab", D);
    check("minor 0, the whole device", "ro emos.system=/dev/block/mmcblk0p0", D);
    check("out of range", "ro emos.system=/dev/block/mmcblk0p999", D);
    check("absurdly long value",
          "ro emos.system=/dev/block/mmcblk0p"
          "111111111111111111111111111111111111111111111111111111111111111111", D);

    /* The two real partitions this ever names, so the check fails if the
     * range test above is ever tightened past them. */
    check("system_a", "ro emos.system=/dev/block/mmcblk0p13", 13);
    check("system_b", "ro emos.system=/dev/block/mmcblk0p14", 14);

    printf("\n%s\n", fails ? "FAILED" : "all ok");
    return fails ? 1 : 0;
}
