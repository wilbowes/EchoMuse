/* Prove init.c puts an unconfirmed WiFi change back (wifi_undo, #1163).
 *
 * The firmware undoes a change it never confirmed, but only once it runs, and
 * it runs only after the network is up: a new network that gives no address
 * stranded the Echo. init now restores the backed-up conf itself. A wrong
 * answer here is silent on hardware -- an Echo on the wrong network shows
 * nothing anyone can reach -- so it is checked off-target, against the real
 * function.
 *
 *   cc -O2 -o wifiundocheck wifiundocheck.c && ./wifiundocheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LEDDIR "/tmp/emos-wifiundocheck"
#define main   init_main_unused

#include "init.c"

#undef main

#define D       "/tmp/emos-wifiundocheck.d"
#define MARK    D "/wifi_pending"
#define BAK     D "/wpa.conf.echomuse-bak"
#define BAKOLD  D "/wpa_supplicant.conf.echomuse-bak"
#define CONF    D "/wpa.conf"
#define CUR     D "/cur.conf"

static int failures;

static void put(const char *path, const char *body, mode_t mode)
{
    FILE *f = fopen(path, "w");
    if (!f) { printf("FAIL  cannot write %s\n", path); exit(1); }
    fputs(body, f);
    fclose(f);
    chmod(path, mode);
}

static int has(const char *path, const char *body)
{
    char b[256] = {0};
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    size_t n = fread(b, 1, sizeof b - 1, f);
    fclose(f);
    b[n] = 0;
    return strcmp(b, body) == 0;
}

static void reset(void)
{
    unlink(MARK); unlink(BAK); unlink(BAKOLD); unlink(CONF); unlink(CUR);
    unlink(CONF ".undo");
    mkdir(D, 0755);
    put(CONF, "network={ssid=\"new\"}\n", 0640);
    put(CUR, "x", 0640);
}

static void expect(const char *label, int cond)
{
    printf("%s  %s\n", cond ? "ok  " : "FAIL", label);
    if (!cond) failures++;
}

int main(void)
{
    reset();
    put(BAK, "network={ssid=\"old\"}\n", 0600);
    expect("no marker: nothing undone",
           wifi_undo(MARK, BAK, BAKOLD, CONF, CUR) == 0
           && has(CONF, "network={ssid=\"new\"}\n"));

    reset();
    put(MARK, "{}", 0644);
    expect("marker without a backup: nothing undone",
           wifi_undo(MARK, BAK, BAKOLD, CONF, CUR) == 0
           && has(CONF, "network={ssid=\"new\"}\n"));

    reset();
    put(MARK, "{}", 0644);
    put(BAK, "network={ssid=\"old\"}\n", 0600);
    struct stat st;
    int r = wifi_undo(MARK, BAK, BAKOLD, CONF, CUR);
    expect("marker and backup: the old conf is back",
           r == 1 && has(CONF, "network={ssid=\"old\"}\n"));
    expect("its mode is the running conf's, so the supplicant can read it",
           stat(CONF, &st) == 0 && (st.st_mode & 07777) == 0640);
    expect("marker and backup are left for the firmware to report",
           access(MARK, F_OK) == 0 && access(BAK, F_OK) == 0);
    expect("no temp file left behind", access(CONF ".undo", F_OK) != 0);

    reset();
    put(MARK, "{}", 0644);
    put(BAKOLD, "network={ssid=\"older\"}\n", 0600);
    expect("only the pre-2026-11 backup: that one is restored",
           wifi_undo(MARK, BAK, BAKOLD, CONF, CUR) == 1
           && has(CONF, "network={ssid=\"older\"}\n"));

    reset();
    put(MARK, "{}", 0644);
    put(BAK, "", 0600);
    expect("an empty backup is refused, the conf untouched",
           wifi_undo(MARK, BAK, BAKOLD, CONF, CUR) == -1
           && has(CONF, "network={ssid=\"new\"}\n")
           && access(CONF ".undo", F_OK) != 0);

    if (failures) {
        printf("\n%d failure(s)\n", failures);
        return 1;
    }
    printf("\nall wifi_undo checks passed\n");
    return 0;
}
