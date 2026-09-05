/* Prove init.c's SHA-256 and password folding agree with the controller's.
 *
 * The two implementations are written in different languages by different
 * hands and can only be known to match by hashing the same inputs. If they
 * drift, the device refuses a password the dashboard just told it to accept,
 * and NOTHING else in either tree notices — the failure surfaces as "the
 * console won't let me in" on hardware, which is the worst place to find it.
 *
 * init.c is included whole, the same trick ringsim.c uses, so this drives the
 * real functions rather than a copy of them.
 *
 *   cc -O2 -o pwcheck pwcheck.c && ./pwcheck
 *
 * It prints `<iterations>:<salt hex>:<hash hex>` records that
 * controller/tests/test_console_pw.py must reproduce exactly.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LEDDIR "/tmp/emos-pwcheck"
#define main   init_main_unused

#include "init.c"

#undef main

static void emit(const char *pw, const char *salthex, long iters)
{
    unsigned char salt[32], out[32];
    int saltlen = unhex(salthex, salt, sizeof salt);
    if (saltlen <= 0) {
        printf("BAD SALT %s\n", salthex);
        return;
    }
    pw_hash(salt, saltlen, pw, iters, out);
    /* The password leads the line, tab-separated, so a reader has every input
     * that produced the record and can recompute it without being told what
     * the inputs were. CI does exactly that: without this field the checker
     * would need its own copy of the table below, which is the drift this
     * file exists to catch rather than commit. A password may contain spaces,
     * so the separator is a tab. */
    printf("%s\t%ld:%s:", pw, iters, salthex);
    for (int i = 0; i < 32; i++)
        printf("%02x", out[i]);
    printf("\n");
}

int main(void)
{
    /* Same inputs as KNOWN_VECTORS in controller/tests/test_console_pw.py.
     * Low iteration counts on purpose: a mistake in the LOOP shows up here
     * rather than being buried under a hundred thousand identical rounds. */
    emit("hunter2", "0001020304050607", 1);
    emit("hunter2", "0001020304050607", 2);
    emit("pw",      "aabbccdd00112233", 3);
    /* One at the real count, to confirm the loop is not quietly quadratic or
     * capped somewhere. */
    emit("correct horse battery staple", "0f1e2d3c4b5a6978", 100000);
    return 0;
}
