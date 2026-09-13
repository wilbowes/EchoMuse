/* Prove init.c finds the supplicant's control socket in the conf it was given.
 *
 * The directory is declared inside wpa_supplicant.conf, and emOS now has two
 * confs that declare different ones: em-wifi writes /data/emos/sockets into
 * emOS's own file, the provisioning wizard writes /data/misc/wifi/sockets into
 * Amazon's, and wpa_conf() switches between them on which exists. Reading the
 * wrong one is silent in the worst way — the only caller is the reassociate
 * nudge in net_main, so a wrong directory does not fail loudly, it just leaves
 * the supplicant sitting at wpa_state=DISCONNECTED for minutes on a device
 * whose credentials are perfectly good. That is indistinguishable on the ring
 * from a wrong password.
 *
 * Both spellings have to work, because both are in the field: hostap's
 * "DIR=/path GROUP=wifi" form that Amazon's conf carries, and the bare path
 * ours writes.
 *
 * init.c is included whole, the same trick ringsim.c, pwcheck.c and
 * tmoutcheck.c use, so this drives the real function rather than a copy of it.
 *
 *   cc -O2 -o wpacheck wpacheck.c && ./wpacheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LEDDIR "/tmp/emos-wpacheck"
#define main   init_main_unused

#include "init.c"

#undef main

#define CONF "/tmp/emos-wpacheck.conf"

static int failures;

/* Write `body` as the conf, then assert which directory init.c reads out of
 * it. `body` NULL means no file at all — an unprovisioned device, which must
 * fall back rather than produce an empty argument. */
static void check(const char *label, const char *body, const char *want)
{
    unlink(CONF);
    if (body) {
        FILE *f = fopen(CONF, "w");
        if (!f) {
            printf("FAIL  %s: could not write the conf\n", label);
            failures++;
            return;
        }
        fwrite(body, 1, strlen(body), f);
        fclose(f);
    }

    char got[128];
    wpa_ctrl_dir(CONF, got, sizeof got);
    if (strcmp(got, want) == 0) {
        printf("ok    %-46s -> %s\n", label, got);
    } else {
        printf("FAIL  %-46s -> %s, want %s\n", label, got, want);
        failures++;
    }
}

#define ANDROID_DIR "/data/misc/wifi/sockets"
#define EMOS_DIR    "/data/emos/sockets"

int main(void)
{
    /* The two confs that actually exist on devices today. */
    check("emOS's own conf, bare path",
          "ctrl_interface=" EMOS_DIR "\nupdate_config=1\n"
          "network={\n\tssid=\"x\"\n}\n", EMOS_DIR);
    check("the wizard's skeleton",
          "ctrl_interface=" ANDROID_DIR "\nupdate_config=1\n", ANDROID_DIR);
    check("Amazon's DIR= form with a GROUP",
          "ctrl_interface=DIR=" ANDROID_DIR " GROUP=wifi\n", ANDROID_DIR);
    check("DIR= form with no GROUP",
          "ctrl_interface=DIR=" EMOS_DIR "\n", EMOS_DIR);

    /* Position and surroundings must not matter: nothing guarantees this is
     * the first line, and the firmware's composeConf puts a dozen globals
     * above it. */
    check("not the first line",
          "update_config=1\nap_scan=1\nctrl_interface=" EMOS_DIR "\n", EMOS_DIR);
    check("leading whitespace",
          "  \tctrl_interface=" EMOS_DIR "\n", EMOS_DIR);
    check("trailing whitespace",
          "ctrl_interface=" EMOS_DIR "   \n", EMOS_DIR);
    check("no trailing newline",
          "ctrl_interface=" EMOS_DIR, EMOS_DIR);
    check("inside a network block's reach",
          "network={\n\tssid=\"x\"\n}\nctrl_interface=" EMOS_DIR "\n", EMOS_DIR);

    /* Last wins, because that is what hostap does: it processes globals line
     * by line and simply overwrites. Stopping at the first match would make
     * init and the supplicant disagree about the same file. */
    check("two declarations, last wins",
          "ctrl_interface=" ANDROID_DIR "\nctrl_interface=" EMOS_DIR "\n",
          EMOS_DIR);

    /* Everything else falls back to Android's directory — which is what every
     * conf written before this existed declared, so the fallback is the old
     * behaviour rather than a guess. */
    check("absent conf",                        NULL,               ANDROID_DIR);
    check("empty conf",                         "",                 ANDROID_DIR);
    check("no ctrl_interface at all",
          "update_config=1\nnetwork={\n\tssid=\"x\"\n}\n",           ANDROID_DIR);
    check("empty value",                        "ctrl_interface=\n", ANDROID_DIR);
    check("DIR= with an empty value",           "ctrl_interface=DIR=\n", ANDROID_DIR);

    /* A relative value means hostap's abstract socket namespace, which
     * `wpa_cli -p` cannot address at all. Passing it on would produce a
     * plausible-looking argument that can never connect, so the default is
     * the better answer. */
    check("relative value (abstract namespace)",
          "ctrl_interface=wpa_ctrl\n",                               ANDROID_DIR);
    check("DIR= with a relative value",
          "ctrl_interface=DIR=wpa_ctrl GROUP=wifi\n",                 ANDROID_DIR);

    /* Near misses. A prefix match on a different key would silently read
     * somebody else's value. */
    check("a longer key that starts the same",
          "ctrl_interface_group=wifi\n",                              ANDROID_DIR);
    check("the key as a comment",
          "#ctrl_interface=" EMOS_DIR "\n",                           ANDROID_DIR);

    unlink(CONF);
    if (failures) {
        printf("\n%d check(s) failed\n", failures);
        return 1;
    }
    printf("\nall ok\n");
    return 0;
}
