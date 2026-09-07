/* Prove init.c reads the console idle timeout record the way it must.
 *
 * The parser is hand-rolled C over a file the FIRMWARE writes and INIT reads,
 * and the two halves are in different languages in different trees. Every way
 * it can be wrong is silent on hardware:
 *
 *   - reading a SHORTER timeout than was set presents as the device dropping
 *     the link mid-session, which nobody would attribute to this file;
 *   - reading a timeout where none was set logs the operator out of a console
 *     they deliberately left open;
 *   - accepting a corrupt record at all is the one failure that must never
 *     happen toward "console stops working", because the console is what you
 *     reach for when everything else has.
 *
 * init.c is included whole, the same trick ringsim.c and pwcheck.c use, so
 * this drives the real function rather than a copy of it.
 *
 *   cc -O2 -o tmoutcheck tmoutcheck.c && ./tmoutcheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LEDDIR        "/tmp/emos-tmoutcheck"
/* The record lives under /data on a device. Redirected here so the check
 * runs anywhere — as a non-root CI runner on a machine with no /data. */
#define CONSOLE_TMOUT "/tmp/emos-tmoutcheck.timeout"
#define main   init_main_unused

#include "init.c"

#undef main

static int failures;

/* Write `body` to the record path, then assert what init.c makes of it.
 * `body` NULL means no file at all, which is the ordinary "never configured"
 * case and must read as no timeout. */
static void check(const char *label, const char *body, long want)
{
    unlink(CONSOLE_TMOUT);
    if (body) {
        FILE *f = fopen(CONSOLE_TMOUT, "w");
        if (!f) {
            printf("FAIL  %s: could not write the record\n", label);
            failures++;
            return;
        }
        fwrite(body, 1, strlen(body), f);
        fclose(f);
    }

    long got = console_timeout_secs();
    if (got == want) {
        printf("ok    %-42s -> %lds\n", label, got);
    } else {
        printf("FAIL  %-42s -> %lds, want %lds\n", label, got, want);
        failures++;
    }
}

int main(void)
{
    /* The ordinary cases. Minutes in, seconds out: the conversion lives at
     * this one point of use so the stored value and the number on the
     * dashboard never disagree by a factor of sixty. */
    check("absent record means no timeout",        NULL,        0);
    check("1 minute",                              "1\n",      60);
    check("15 minutes",                            "15\n",    900);
    check("90 minutes, the ceiling",               "90\n",   5400);
    check("no trailing newline",                   "5",       300);
    check("trailing spaces",                       "5   \n",  300);

    /* Zero is the explicit no-timeout value. It is a CHOICE rather than an
     * absence, and both must resolve the same way here. */
    check("0 means no timeout",                    "0\n",       0);

    /* Everything malformed falls toward NO timeout. A console that stops
     * working is worse than one that stays open, because it is what somebody
     * reaches for when the rest of the device has already failed. */
    check("empty file",                            "",          0);
    check("whitespace only",                       "   \n",     0);
    check("not a number",                          "forever\n", 0);
    check("negative",                              "-5\n",      0);
    check("float",                                 "1.5\n",     0);
    check("leading space",                         " 5\n",      0);
    check("number with a suffix",                  "5m\n",      0);

    /* Over the ceiling is refused rather than clamped. A value of 600 is
     * somebody who meant seconds, and silently giving them ten hours is a
     * console left open all day by a setting that looked accepted. */
    check("91 minutes, just over",                 "91\n",      0);
    check("600, likely seconds by mistake",        "600\n",     0);
    check("absurdly large, must not overflow",     "999999999999999999999\n", 0);

    unlink(CONSOLE_TMOUT);
    if (failures) {
        printf("\n%d check(s) failed\n", failures);
        return 1;
    }
    printf("\nall ok\n");
    return 0;
}
