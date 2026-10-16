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

/* cmdline_board() returns a pointer to a string (possibly BOARD_DEFAULT,
 * possibly a static buffer of the parsed id), not an integer. A separate
 * helper keeps the system-part test above unchanged while covering the
 * board-id parser with the same ok/FAIL shape.
 *
 * strcmp() rather than pointer equality because the parser may return a
 * pointer into its own buffer, not a pointer to a string literal -- and
 * even when both sides ARE literals, what the test asserts is "the id
 * reads as this name", which is value equality. */
static void check_str(const char *what, const char *cmdline, const char *want)
{
    const char *got = cmdline_board(cmdline);
    if (got && !strcmp(got, want)) {
        printf("ok    %-46s -> %s\n", what, got);
    } else {
        printf("FAIL  %-46s -> %s, wanted %s\n", what,
               got ? got : "(null)", want ? want : "(null)");
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

    /* ── cmdline_board() ────────────────────────────────────────────────────
     *
     * Same shape as the system-part parser: stamp recognised, stamp absent,
     * stamp malformed, stamp that names a board we have never heard of. The
     * last case falls back to the default rather than refusing — an unknown
     * id is the device we have not built support for yet, and a refusal is a
     * device that cannot be brought up to ask.
     *
     * The default value is BOARD_DEFAULT ("biscuit"), pinned by the constant
     * rather than by string equality here, so renaming it changes one line
     * rather than every test case. */
    const char *BD = BOARD_DEFAULT;

    printf("\n--- cmdline_board() ---\n");

    /* Absent: an image built before this stamp existed keeps booting as it
     * did, against the only board init currently knows. */
    check_str("absent -- an older image", "bootopt=64S3 ro init=/init", BD);
    check_str("empty cmdline", "", BD);

    /* Stamped: the board id travels verbatim from the packer to init. */
    check_str("stamped, biscuit",
              "bootopt=64S3 ro init=/init emos.board=biscuit", "biscuit");
    check_str("stamped, radar",
              "bootopt=64S3 ro init=/init emos.board=radar", "radar");
    check_str("stamped first",
              "emos.board=donut bootopt=64S3 ro", "donut");
    check_str("stamped in the middle",
              "ro emos.board=echo3 init=/init", "echo3");
    check_str("trailing newline, as /proc/cmdline gives it",
              "ro emos.board=biscuit\n", "biscuit");
    check_str("stamped alongside emos.system=",
              "ro emos.system=/dev/block/mmcblk0p14 emos.board=biscuit",
              "biscuit");
    /* The packer's full stamp: both keys appear in the order it writes
     * them, on a real device's cmdline. */
    check_str("full packer stamp",
              "bootopt=64S3 ramoops.dump_oops=1 "
              "emos.system=/dev/block/mmcblk0p13 emos.board=radar",
              "radar");

    /* Token boundary: a longer key merely ENDING in ours cannot answer. */
    check_str("a longer key ending in ours",
              "ro xemos.board=biscuit", BD);
    check_str("our key as a value of something else",
              "ro other=emos.board=biscuit", BD);

    /* Anything we do not recognise falls back rather than being interpreted
     * generously -- an id from a builder we do not know is not a licence to
     * guess. The fallback keeps the device bootable for diagnosis, which is
     * what "unknown" buys elsewhere in this file. */
    check_str("unknown id", "ro emos.board=donut-v2", "donut-v2");
    check_str("unknown id is left intact, not defaulted",
              "ro emos.board=somethingwehavenot", "somethingwehavenot");

    /* Reject anything that is not printable ASCII. A board id is a string the
     * packer stamps and init selects on, and a non-printable value here is
     * corruption rather than intent. */
    check_str("non-printable at the end", "ro emos.board=biscuit\x01", BD);
    /* A bare space in the value would END the token, so this is not a
     * useful test of non-printable rejection (the parser would simply
     * see a shorter value). A \x01 mid-token survives tokenisation, so
     * the parser's printable-ASCII check actually fires. */
    check_str("non-printable mid-value", "ro emos.board=bis\x01cuit", BD);
    check_str("the key with an empty value", "ro emos.board=", BD);
    check_str("the key with no '='", "ro emos.board", BD);
    /* A bare substring search would return 'biscuit' here too — the test
     * names the failure so a future refactor cannot re-introduce it. */
    check_str("value longer than the buffer",
              "ro emos.board=" /* pad out to 65 chars */
              "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz"
              "abcdefghijklmnop", BD);

    printf("\n%s\n", fails ? "FAILED" : "all ok");
    return fails ? 1 : 0;
}
