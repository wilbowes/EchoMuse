/* Prove init.c decides "is this image on trial" the way the controller means.
 *
 * The trial mark is written by the controller (controller/em_emos_update.py,
 * Python) and read by init (C), and it decides whether a freshly flashed image
 * is rolled back. Both ways of being wrong are silent on hardware:
 *
 *   - reading a mark as ABSENT confirms an updated image at network-up, so one
 *     that cannot run the firmware is promoted and needs a cable;
 *   - reading a LEFTOVER mark as live puts a healthy image on trial with no
 *     controller waiting to confirm it, and it is rolled back ten minutes on.
 *
 * init.c is included whole, as ringsim.c and pwcheck.c do, so this drives the
 * real functions. tests/test_emos_update.py pins the same vector from the
 * Python side.
 *
 *   cc -O2 -o trialcheck trialcheck.c && ./trialcheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LEDDIR    "/tmp/emos-trialcheck"
#define BOOTDEV   "/tmp/emos-trialcheck.boot"
#define TRIALMARK "/tmp/emos-trialcheck.pending"
#define GOODIMG   "/tmp/emos-trialcheck.good"
#define BOOTSTATE "/tmp/emos-trialcheck.state"
#define ROLLBACKREC "/tmp/emos-trialcheck.rollback"
#define main   init_main_unused

#include "init.c"

#undef main

static int failures;

/* The shared vector: id bytes 00..13, as test_emos_update.py has them. */
#define ID_HEX "000102030405060708090a0b0c0d0e0f10111213"

/* A minimal boot image: a header init accepts, carrying `id` at offset 576. */
static void write_boot(int valid)
{
    unsigned char img[4096];
    memset(img, 0, sizeof img);
    if (valid) {
        unsigned ps = 2048;
        memcpy(img, "ANDROID!", 8);
        memcpy(img + 36, &ps, 4);
    }
    for (int i = 0; i < 20; i++)
        img[576 + i] = (unsigned char)i;
    FILE *f = fopen(BOOTDEV, "w");
    fwrite(img, 1, sizeof img, f);
    fclose(f);
}

static void check(const char *label, const char *mark, int want, int kept)
{
    unlink(TRIALMARK);
    if (mark) {
        FILE *f = fopen(TRIALMARK, "w");
        fwrite(mark, 1, strlen(mark), f);
        fclose(f);
    }
    int got = trial_state();
    int there = access(TRIALMARK, F_OK) == 0;
    if (got == want && there == kept) {
        printf("ok    %s\n", label);
    } else {
        printf("FAIL  %s: state %d (want %d), mark %s (want %s)\n", label,
               got, want, there ? "kept" : "removed", kept ? "kept" : "removed");
        failures++;
    }
}

int main(void)
{
    write_boot(1);

    check("no mark is no trial",              NULL, TRIAL_NONE, 0);
    check("the running image's id",           ID_HEX "\n", TRIAL_ACTIVE, 1);
    check("no trailing newline",              ID_HEX, TRIAL_ACTIVE, 1);
    check("CRLF, as a console paste leaves",  ID_HEX "\r\n", TRIAL_ACTIVE, 1);
    check("the controller's notes after it",
          ID_HEX "\nbuild=0123456789abcdef\nversion=0.10\n", TRIAL_ACTIVE, 1);

    /* Leftovers and damage: never a trial, and never left to become one. */
    check("another image's id",
          "ffffffffffffffffffffffffffffffffffffffff\n", TRIAL_STALE, 0);
    check("empty file",                       "", TRIAL_STALE, 0);
    check("one digit short",
          "000102030405060708090a0b0c0d0e0f1011121\n", TRIAL_STALE, 0);
    check("a longer digest that starts the same", ID_HEX "14\n", TRIAL_STALE, 0);
    check("uppercase hex",
          "000102030405060708090A0B0C0D0E0F10111213\n", TRIAL_STALE, 0);
    check("leading space",                    " " ID_HEX "\n", TRIAL_STALE, 0);
    check("not hex at all",
          "the quick brown fox jumps over the lazy dog!\n", TRIAL_STALE, 0);

    /* Nothing bootable to compare against: the id cannot be trusted. */
    write_boot(0);
    check("boot partition is not an image",   ID_HEX "\n", TRIAL_STALE, 0);
    unlink(BOOTDEV);
    check("boot partition unreadable",        ID_HEX "\n", TRIAL_STALE, 0);

    /* trial_pending() is asked of the FILE, so removing it ends the trial. */
    write_boot(1);
    check("re-arm for the pending check",     ID_HEX "\n", TRIAL_ACTIVE, 1);
    on_trial = 1;
    if (!trial_pending()) { printf("FAIL  pending with the mark present\n"); failures++; }
    unlink(TRIALMARK);
    if (trial_pending())  { printf("FAIL  pending after the mark was removed\n"); failures++; }
    else printf("ok    removing the mark ends the trial\n");
    on_trial = 0;

    /* The rollback record: what the controller reads to say a rollback
     * happened. The same text is in tests/test_emos_update.py. */
    {
        static const char want[] =
            "from=" ID_HEX "\n"
            "to=unknown\n"
            "tries=3\n";
        char got[200] = {0};
        write_boot(1);
        unlink(GOODIMG);
        unlink(ROLLBACKREC);
        write_rollback_record(3);
        FILE *f = fopen(ROLLBACKREC, "r");
        if (f) { size_t n = fread(got, 1, sizeof got - 1, f); (void)n; fclose(f); }
        if (strcmp(got, want)) {
            printf("FAIL  rollback record with no known-good:\n%s", got);
            failures++;
        } else
            printf("ok    rollback record names the failed image\n");

        /* Both readable: the restored image is named too. */
        rename(BOOTDEV, GOODIMG);
        write_boot(1);
        write_rollback_record(3);
        memset(got, 0, sizeof got);
        f = fopen(ROLLBACKREC, "r");
        if (f) { size_t n = fread(got, 1, sizeof got - 1, f); (void)n; fclose(f); }
        if (strcmp(got, "from=" ID_HEX "\nto=" ID_HEX "\ntries=3\n")) {
            printf("FAIL  rollback record with both images:\n%s", got);
            failures++;
        } else
            printf("ok    rollback record names the restored image\n");
        unlink(GOODIMG);
        unlink(ROLLBACKREC);
    }

    unlink(BOOTDEV);
    unlink(TRIALMARK);
    if (failures) {
        printf("\n%d FAILED\n", failures);
        return 1;
    }
    printf("\nall trial-mark checks passed\n");
    return 0;
}
