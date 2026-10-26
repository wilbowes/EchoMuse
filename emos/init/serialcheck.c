/* Prove init.c resolves the device serial, and refuses a corrupt one; and that
 * it tells radar from biscuit by idme's device type, which picks radar's WiFi
 * bring-up (a wrong answer is a radar with no WiFi, or biscuit sent radar's
 * ioctls).
 *
 * The whole fleet is keyed on the serial. Getting it wrong is the quiet kind of
 * wrong: a device that cannot resolve one registers as "unknown", and a second
 * device doing the same collides with the first — two units sharing an
 * identity, each overwriting the other's rows, with nothing anywhere reporting
 * it. A MANGLED serial is worse still, because it looks like a real device.
 *
 * It is also a parser over strings written by other people — Amazon's kernel
 * driver exports /proc/idme/serial, and LK appends androidboot.serialno to the
 * cmdline — which is this project's stated criterion for owing a check.
 *
 * The cmdline source is here because it FAILED on FireOS 6 and the failure was
 * invisible. That kernel is 32-bit, so COMMAND_LINE_SIZE is 1024; our image
 * cmdline is 385 bytes against stock's 70, which pushes androidboot.serialno to
 * byte 1040 and off the end. The parse was correct the whole time and there was
 * simply nothing to find, which is why idme now leads.
 *
 * init.c is included whole, the same trick ringsim.c, pwcheck.c, tmoutcheck.c,
 * wpacheck.c and cmdlinecheck.c use, so this drives the real functions rather
 * than a copy.
 *
 *   cc -O2 -o serialcheck serialcheck.c && ./serialcheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LEDDIR "/tmp/emos-serialcheck"
#define main   init_main_unused

#include "init.c"

#undef main

static int fails;

static void want_copy(const char *what, const char *raw, const char *expect)
{
    char got[64];
    serial_copy(raw, got, sizeof got);
    if (strcmp(got, expect) == 0) {
        printf("ok    %-44s -> \"%s\"\n", what, got);
    } else {
        printf("FAIL  %-44s -> \"%s\", wanted \"%s\"\n", what, got, expect);
        fails++;
    }
}

static void want_cmdline(const char *what, const char *line, const char *expect)
{
    char got[64];
    serial_from_cmdline(line, got, sizeof got);
    if (strcmp(got, expect) == 0) {
        printf("ok    %-44s -> \"%s\"\n", what, got);
    } else {
        printf("FAIL  %-44s -> \"%s\", wanted \"%s\"\n", what, got, expect);
        fails++;
    }
}

/* Read off G090LF11752215LE on 2026-09-15. */
#define REAL "G090LF11752215LE"

int main(void)
{
    printf("serial_copy() -- what /proc/idme/serial hands us\n");

    /* procfs returns this with NO trailing newline, which is the case a test
     * written against an ordinary file would never produce. */
    want_copy("idme, exactly as the kernel gives it", REAL, REAL);
    want_copy("with a trailing newline", REAL "\n", REAL);
    want_copy("with a trailing NUL", REAL "\0", REAL);
    want_copy("with trailing space", REAL " ", REAL);
    want_copy("value then more text", REAL " extra", REAL);

    /* Absent or unpopulated must read as absent, so the caller falls through
     * to the cmdline rather than registering something. */
    want_copy("empty", "", "");
    want_copy("a lone newline", "\n", "");
    want_copy("a lone space", " ", "");

    /* Corrupt is refused outright. A field half-written by a driver, or a
     * partition read that returned binary, must not become an identity. */
    want_copy("a control character", "G090\x01LF117", "");
    want_copy("a high byte", "G090\xc3LF117", "");
    want_copy("a tab", "G090\tLF117", "");
    want_copy("all NULs", "\0\0\0", "");

    /* Longer than the buffer truncates rather than overflowing. 63 chars fit;
     * the 64th is dropped and the result stays NUL-terminated. */
    {
        char big[80];
        memset(big, 'A', sizeof big);
        big[sizeof big - 1] = 0;
        char got[64];
        serial_copy(big, got, sizeof got);
        if (strlen(got) == 63) {
            printf("ok    %-44s -> %zu chars\n", "over-long input truncates safely",
                   strlen(got));
        } else {
            printf("FAIL  %-44s -> %zu chars, wanted 63\n",
                   "over-long input truncates safely", strlen(got));
            fails++;
        }
    }

    printf("\nserial_from_cmdline() -- the fallback\n");

    want_cmdline("a normal cmdline",
                 "console=tty0 androidboot.serialno=" REAL " ro", REAL);
    want_cmdline("the serial last, with no trailing space",
                 "console=tty0 androidboot.serialno=" REAL, REAL);
    want_cmdline("the serial first",
                 "androidboot.serialno=" REAL " console=tty0", REAL);

    /* The v2 case that started this: the argument is gone entirely, cut off at
     * COMMAND_LINE_SIZE. Finding nothing is the correct answer. */
    want_cmdline("truncated away at 1024 bytes",
                 "console=tty0 vmalloc=496M bootprof.lk_t=409", "");

    want_cmdline("absent", "console=tty0 ro", "");
    want_cmdline("present but empty", "ro androidboot.serialno= x", "");

    /* Must match the whole key, not a suffix of a longer one -- the same trap
     * cmdlinecheck.c guards for emos.system=. */
    want_cmdline("a longer key ending in ours",
                 "ro xandroidboot.serialno=" REAL, "");
    want_cmdline("our key as a value of something else",
                 "ro other=androidboot.serialno=" REAL, "");
    want_cmdline("a decoy before the real one",
                 "ro xandroidboot.serialno=DECOY androidboot.serialno=" REAL, REAL);

    printf("\nboard_of() -- /proc/idme/device_type_id\n");
    {
        static const char *const name[] = { "biscuit", "radar", "donut" };
        static const struct { const char *what, *raw; enum board want; } t[] = {
            /* Read off 035T on 2026-10-23: 12 characters and a NUL. */
            { "radar, as the kernel gives it", "A7WXQPH584YP\0", BOARD_RADAR },
            { "radar with a newline",          "A7WXQPH584YP\n", BOARD_RADAR },
            /* Read off 41C8 on 2026-10-25. */
            { "donut, as the kernel gives it", "A32DOYMUN6DTXA\0", BOARD_DONUT },
            { "donut with a newline",          "A32DOYMUN6DTXA\n", BOARD_DONUT },
            { "biscuit",                       "A3S5BH2HU6VAYF\0", BOARD_BISCUIT },
            { "radar's id with a suffix",      "A7WXQPH584YPX", BOARD_BISCUIT },
            { "a prefix of radar's id",        "A7WXQPH584Y", BOARD_BISCUIT },
            { "a prefix of donut's id",        "A32DOYMUN6DTX", BOARD_BISCUIT },
            { "lower case",                    "a7wxqph584yp", BOARD_BISCUIT },
            { "a leading NUL",                 "\0A7WXQPH584YP", BOARD_BISCUIT },
            { "empty",                         "", BOARD_BISCUIT },
        };
        for (size_t i = 0; i < sizeof t / sizeof t[0]; i++) {
            enum board got = board_of(t[i].raw);
            printf("%s  %-44s -> %s\n", got == t[i].want ? "ok  " : "FAIL",
                   t[i].what, name[got]);
            fails += got != t[i].want;
        }
    }

    printf("\n%s\n", fails ? "FAILED" : "all ok");
    return fails ? 1 : 0;
}
