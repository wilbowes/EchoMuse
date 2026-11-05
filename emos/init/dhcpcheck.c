/* Prove init.c asks the DHCP client to check the address it is handed (#871).
 *
 * Both clients hand over whatever address the server offers unless told
 * otherwise, and both ways of losing the flag are silent on hardware: the Echo
 * keeps a duplicate address, every connection to the controller is reset, and
 * the only clue is mDNS "failed verification" — which reads as a controller
 * problem, not as a network one. It is what the #871 device showed for a day.
 *
 * udhcpc needs -a (ARP-probe the offered address, DHCPDECLINE a conflict,
 * RFC 2131 §4.4.1); dhcpcd needs -A ABSENT, because -A is --noarp, and must
 * keep -L so that dropping -A does not also re-enable its IPv4LL fallback.
 *
 * init.c is included whole, the same trick wpacheck.c and dnscheck.c use, so
 * this drives the real argv rather than a copy of it.
 *
 *   cc -O2 -o dhcpcheck dhcpcheck.c && ./dhcpcheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LEDDIR "/tmp/emos-dhcpcheck"
#define main   init_main_unused

#include "init.c"

#undef main

static int failures;

static void expect(const char *label, int ok)
{
    printf(ok ? "ok    %s\n" : "FAIL  %s\n", label);
    if (!ok)
        failures++;
}

static int has(char *const *argv, const char *want)
{
    for (int i = 0; argv[i]; i++)
        if (strcmp(argv[i], want) == 0)
            return 1;
    return 0;
}

/* A short option, bundled or not: -A may come back as "-ABK". */
static int has_flag(char *const *argv, char c)
{
    for (int i = 0; argv[i]; i++) {
        if (argv[i][0] != '-' || argv[i][1] == '-')
            continue;
        if (strchr(argv[i] + 1, c))
            return 1;
    }
    return 0;
}

int main(void)
{
    char **fos6 = dhcp_argv(1);
    char **fos5 = dhcp_argv(0);

    expect("FireOS 6 runs udhcpc",                     strcmp(fos6[0], "/sbin/udhcpc") == 0);
    expect("udhcpc probes the offer (-a)",             has_flag(fos6, 'a'));
    expect("udhcpc keeps its lease script",            has(fos6, "-s") && has(fos6, UDHCPC_SCRIPT));
    expect("udhcpc still runs on wlan0",               has(fos6, "-i") && has(fos6, "wlan0"));

    expect("FireOS 5 runs dhcpcd",                     strcmp(fos5[0], "/system/bin/dhcpcd") == 0);
    expect("dhcpcd keeps its ARP check (no -A)",       !has_flag(fos5, 'A'));
    expect("no long --noarp either",                   !has(fos5, "--noarp"));
    expect("dhcpcd keeps IPv4LL off (-L)",             has_flag(fos5, 'L'));
    expect("dhcpcd keeps its conf",                    has(fos5, "-f")
                                                       && has(fos5, "/system/etc/dhcpcd/dhcpcd.conf"));
    expect("dhcpcd still runs on wlan0",               has(fos5, "wlan0"));

    if (failures) {
        printf("\n%d check(s) failed\n", failures);
        return 1;
    }
    printf("\nall ok\n");
    return 0;
}
