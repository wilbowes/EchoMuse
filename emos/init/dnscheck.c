/* Prove init.c takes DNS from the DHCP lease, by the spec (#1066).
 *
 * A wrong answer here is silent on hardware: the Echo keeps its address and
 * its controller (found over mDNS, reached by IP), and only names stop
 * resolving, which reads as a broken network rather than a broken parser.
 * The cases are RFC 2131's message, RFC 2132's option 6 (a multiple of four
 * bytes, at least four) and option 52 overload, RFC 3396's concatenation of a
 * repeated option, and the resolver's three-server limit. The udhcpc hook is
 * run in a real shell with a fake busybox.
 *
 * init.c is included whole, the same trick wpacheck.c uses, so this drives
 * the real functions rather than copies.
 *
 *   cc -O2 -o dnscheck dnscheck.c && ./dnscheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LEDDIR       "/tmp/emos-dnscheck"
#define RESOLV_CONF  "/tmp/emos-dnscheck.resolv"
#define DHCPCD_LEASE "/tmp/emos-dnscheck.lease"
#define UDHCPC_DNS   "/tmp/emos-dnscheck.udhcpc"
#define DHCPCD_LEASE_DIR "/tmp/emos-dnscheck.d"
#define main         init_main_unused

#include "init.c"

#undef main

static int failures;

static void expect(const char *label, int ok, const char *got)
{
    if (ok)
        printf("ok    %s\n", label);
    else {
        printf("FAIL  %s: %s\n", label, got);
        failures++;
    }
}

/* A BOOTREPLY with `opts` in the options field (no end option added). */
static size_t reply(unsigned char *b, const unsigned char *opts, size_t n)
{
    memset(b, 0, 240);
    b[0] = 2;
    b[236] = 99; b[237] = 130; b[238] = 83; b[239] = 99;
    memcpy(b + 240, opts, n);
    return 240 + n;
}

static void servers(const char *label, const unsigned char *b, size_t n, const char *want)
{
    uint32_t ns[RESOLV_MAXNS];
    char got[256];
    int c = lease_dns(b, n, ns, RESOLV_MAXNS);
    resolv_text(ns, c, "", got, sizeof got);
    expect(label, strcmp(got, want) == 0, got[0] ? got : "(none)");
}

static void text(const char *label, const char *in, const char *want)
{
    uint32_t ns[RESOLV_MAXNS];
    char got[256];
    resolv_text(ns, text_dns(in, ns, RESOLV_MAXNS), "", got, sizeof got);
    expect(label, strcmp(got, want) == 0, got[0] ? got : "(none)");
}

static void slurp(const char *path, char *out, size_t sz)
{
    out[0] = 0;
    FILE *f = fopen(path, "r");
    if (!f)
        return;
    size_t r = fread(out, 1, sz - 1, f);
    out[r] = 0;
    fclose(f);
}

static void put(const char *path, const void *b, size_t n)
{
    FILE *f = fopen(path, "w");
    fwrite(b, 1, n, f);
    fclose(f);
}

int main(void)
{
    unsigned char b[1024];
    size_t n;

    /* --- RFC 2132 3.8: option 6 ------------------------------------------ */
    static const unsigned char one[] = { 53,1,5, 3,4,10,0,0,1, 6,4,10,0,0,53, 255 };
    n = reply(b, one, sizeof one);
    servers("one server", b, n, "nameserver 10.0.0.53\n");

    static const unsigned char three[] = { 6,12, 1,1,1,1, 8,8,8,8, 9,9,9,9, 255 };
    n = reply(b, three, sizeof three);
    servers("three servers, in order", b, n,
            "nameserver 1.1.1.1\nnameserver 8.8.8.8\nnameserver 9.9.9.9\n");

    static const unsigned char five[] = { 6,20, 1,1,1,1, 2,2,2,2, 3,3,3,3, 4,4,4,4, 5,5,5,5, 255 };
    n = reply(b, five, sizeof five);
    servers("more than three: the first three (MAXNS)", b, n,
            "nameserver 1.1.1.1\nnameserver 2.2.2.2\nnameserver 3.3.3.3\n");

    static const unsigned char none[] = { 53,1,5, 3,4,10,0,0,1, 255 };
    n = reply(b, none, sizeof none);
    servers("no option 6", b, n, "");

    static const unsigned char odd[] = { 6,5, 1,1,1,1,1, 255 };
    n = reply(b, odd, sizeof odd);
    servers("length not a multiple of 4: refused", b, n, "");

    static const unsigned char zero[] = { 6,0, 255 };
    n = reply(b, zero, sizeof zero);
    servers("zero-length option 6 (minimum is 4): refused", b, n, "");

    static const unsigned char trunc[] = { 6,8, 1,1,1,1 };
    n = reply(b, trunc, sizeof trunc);
    servers("option runs past the end: refused", b, n, "");

    static const unsigned char trunc_hdr[] = { 6 };
    n = reply(b, trunc_hdr, sizeof trunc_hdr);
    servers("option cut after its code: refused", b, n, "");

    static const unsigned char noend[] = { 6,4, 10,0,0,53 };
    n = reply(b, noend, sizeof noend);
    servers("no end option: the area's end ends it", b, n, "nameserver 10.0.0.53\n");

    static const unsigned char pads[] = { 0,0, 6,4, 10,0,0,53, 0, 255, 6,4, 7,7,7,7 };
    n = reply(b, pads, sizeof pads);
    servers("pads skipped, nothing read after end", b, n, "nameserver 10.0.0.53\n");

    static const unsigned char junk[] = { 6,12, 0,0,0,0, 255,255,255,255, 10,0,0,53, 255 };
    n = reply(b, junk, sizeof junk);
    servers("0.0.0.0 and broadcast dropped", b, n, "nameserver 10.0.0.53\n");

    static const unsigned char dup[] = { 6,8, 10,0,0,53, 10,0,0,53, 255 };
    n = reply(b, dup, sizeof dup);
    servers("duplicate dropped", b, n, "nameserver 10.0.0.53\n");

    /* --- RFC 3396: a repeated option is one option ----------------------- */
    static const unsigned char split[] = { 6,6, 1,1,1,1, 2,2, 3,4,10,0,0,1, 6,2, 2,2, 255 };
    n = reply(b, split, sizeof split);
    servers("option 6 split across instances is joined", b, n,
            "nameserver 1.1.1.1\nnameserver 2.2.2.2\n");

    /* --- RFC 2132 9.3: option 52 overload -------------------------------- */
    static const unsigned char ovl_file[] = { 52,1,1, 255 };
    n = reply(b, ovl_file, sizeof ovl_file);
    { static const unsigned char f[] = { 6,4, 10,0,0,53, 255 }; memcpy(b + 108, f, sizeof f); }
    servers("overload 1: servers in the file field", b, n, "nameserver 10.0.0.53\n");

    static const unsigned char ovl_both[] = { 6,4, 1,1,1,1, 52,1,3, 255 };
    n = reply(b, ovl_both, sizeof ovl_both);
    { static const unsigned char f[] = { 6,4, 2,2,2,2, 255 }; memcpy(b + 108, f, sizeof f); }
    { static const unsigned char s[] = { 6,4, 3,3,3,3, 255 }; memcpy(b + 44, s, sizeof s); }
    servers("overload 3: options, then file, then sname", b, n,
            "nameserver 1.1.1.1\nnameserver 2.2.2.2\nnameserver 3.3.3.3\n");

    static const unsigned char no_ovl[] = { 255 };
    n = reply(b, no_ovl, sizeof no_ovl);
    { static const unsigned char f[] = { 6,4, 10,0,0,53, 255 }; memcpy(b + 108, f, sizeof f); }
    servers("file field ignored without option 52", b, n, "");

    /* --- RFC 2131 message ------------------------------------------------- */
    n = reply(b, one, sizeof one);
    b[0] = 1;
    servers("a BOOTREQUEST is not a lease", b, n, "");
    n = reply(b, one, sizeof one);
    b[239] = 0;
    servers("wrong magic cookie", b, n, "");
    servers("shorter than the fixed header", b, 239, "");

    /* --- udhcpc's $dns ---------------------------------------------------- */
    text("udhcpc: two servers", "10.0.0.53 1.1.1.1\n", "nameserver 10.0.0.53\nnameserver 1.1.1.1\n");
    text("udhcpc: empty", "\n", "");
    text("udhcpc: junk tokens skipped", "abc 1.2.3 1.2.3.4.5 256.1.1.1 1.1.1.1x 9.9.9.9",
         "nameserver 9.9.9.9\n");
    text("udhcpc: four-digit octet skipped", "0010.0.0.1 8.8.8.8", "nameserver 8.8.8.8\n");

    /* --- the fallback ----------------------------------------------------- */
    {
        char got[256];
        resolv_text(NULL, 0, "192.168.1.1", got, sizeof got);
        expect("no servers: the gateway", strcmp(got, "nameserver 192.168.1.1\n") == 0, got);
        expect("no servers, no gateway: nothing to write",
               resolv_text(NULL, 0, "", got, sizeof got) == 0, got);
    }

    /* --- current_dns: which source, and a stale lease ---------------------- */
    {
        uint32_t ns[RESOLV_MAXNS];
        char got[256];
        unlink(UDHCPC_DNS);
        n = reply(b, one, sizeof one);
        put(DHCPCD_LEASE, b, n);
        lease_snapshot();
        resolv_text(ns, current_dns(ns, RESOLV_MAXNS), "", got, sizeof got);
        expect("the lease found at boot is not believed", got[0] == 0, got);
        n = reply(b, three, sizeof three);
        put(DHCPCD_LEASE, b, n);
        resolv_text(ns, current_dns(ns, RESOLV_MAXNS), "", got, sizeof got);
        expect("a lease dhcpcd rewrote is", strncmp(got, "nameserver 1.1.1.1\n", 19) == 0, got);
        put(UDHCPC_DNS, "4.4.4.4\n", 8);
        resolv_text(ns, current_dns(ns, RESOLV_MAXNS), "", got, sizeof got);
        expect("udhcpc's file wins when present", strcmp(got, "nameserver 4.4.4.4\n") == 0, got);
        unlink(DHCPCD_LEASE);
        unlink(UDHCPC_DNS);
        lease_snapshot();
        put(DHCPCD_LEASE, b, n);
        resolv_text(ns, current_dns(ns, RESOLV_MAXNS), "", got, sizeof got);
        expect("no lease at boot, one now: believed", got[0] != 0, "(none)");
        unlink(DHCPCD_LEASE);
    }

    /* --- update_resolv writes the file ----------------------------------- */
    {
        char got[256];
        unlink(RESOLV_CONF);
        put(UDHCPC_DNS, "10.0.0.53\n", 10);
        update_resolv();
        slurp(RESOLV_CONF, got, sizeof got);
        expect("update_resolv writes the lease's server",
               strcmp(got, "nameserver 10.0.0.53\n") == 0, got);
        put(UDHCPC_DNS, "10.0.0.54\n", 10);
        update_resolv();
        slurp(RESOLV_CONF, got, sizeof got);
        expect("a renewal with a new server rewrites it",
               strcmp(got, "nameserver 10.0.0.54\n") == 0, got);
        unlink(UDHCPC_DNS);
        unlink(RESOLV_CONF);
    }

    /* --- the udhcpc hook, in a shell --------------------------------------- */
    {
        char script[1024], got[512];
        const char *bb = "/tmp/emos-dnscheck.bb";
        static const char fake[] = "#!/bin/sh\n[ \"$1\" = mv ] && { shift; exec mv \"$@\"; }\n"
                                  "echo \"$*\" >> /tmp/emos-dnscheck.log\n";
        put(bb, fake, sizeof fake - 1);
        chmod(bb, 0755);
        int sl = udhcpc_hook(script, sizeof script, bb);
        expect("hook fits its buffer with a long busybox path",
               udhcpc_hook(script, sizeof script,
                           "/system/vendor/xbin/busybox-armv7l-static-1.36") < (int)sizeof script, "");
        udhcpc_hook(script, sizeof script, bb);
        /* The hook's #! names /system/bin/sh; run it with the host's. */
        put("/tmp/emos-dnscheck.sh", script, (size_t)sl);
        unlink("/tmp/emos-dnscheck.log");
        unlink(UDHCPC_DNS);
        int rc = system("interface=wlan0 ip=10.0.0.9 subnet=255.255.255.0 "
                        "router='10.0.0.1 10.0.0.2' dns='10.0.0.53 1.1.1.1' "
                        "sh /tmp/emos-dnscheck.sh bound");
        slurp(UDHCPC_DNS, got, sizeof got);
        expect("hook: bound writes $dns", rc == 0 && strcmp(got, "10.0.0.53 1.1.1.1\n") == 0, got);
        slurp("/tmp/emos-dnscheck.log", got, sizeof got);
        expect("hook: replaces the default route with the first router",
               strstr(got, "route del default\nroute add default gw 10.0.0.1\n") != NULL, got);
        system("interface=wlan0 ip=10.0.0.9 subnet=255.255.255.0 router=10.0.0.1 dns= "
               "sh /tmp/emos-dnscheck.sh renew");
        slurp(UDHCPC_DNS, got, sizeof got);
        expect("hook: a lease without DNS empties the file", strcmp(got, "\n") == 0, got);
        put(UDHCPC_DNS, "7.7.7.7\n", 8);
        system("dns=9.9.9.9 sh /tmp/emos-dnscheck.sh deconfig");
        slurp(UDHCPC_DNS, got, sizeof got);
        expect("hook: other events leave it alone", strcmp(got, "7.7.7.7\n") == 0, got);
        unlink(UDHCPC_DNS);
    }

    printf(failures ? "\n%d failure(s)\n" : "\ndnscheck: all ok\n", failures);
    /* --- the lease directory dhcpcd writes into ------------------------- */
    {
        struct stat sb;
        uid_t u = getuid();
        gid_t g = getgid();
        rmdir(DHCPCD_LEASE_DIR);
        expect("lease dir: made when missing",
               lease_dir_ready(DHCPCD_LEASE_DIR, u, g) == 0 && stat(DHCPCD_LEASE_DIR, &sb) == 0
                   && S_ISDIR(sb.st_mode) && (sb.st_mode & 07777) == 0770, "not 0770");
        chmod(DHCPCD_LEASE_DIR, 0700);
        expect("lease dir: mode put right",
               lease_dir_ready(DHCPCD_LEASE_DIR, u, g) == 0 && stat(DHCPCD_LEASE_DIR, &sb) == 0
                   && (sb.st_mode & 07777) == 0770, "mode left wrong");
        struct stat before;
        stat(DHCPCD_LEASE_DIR, &before);
        sleep(1);
        lease_dir_ready(DHCPCD_LEASE_DIR, u, g);
        stat(DHCPCD_LEASE_DIR, &sb);
        expect("lease dir: nothing written when already right",
               sb.st_ctime == before.st_ctime, "ctime moved");
        rmdir(DHCPCD_LEASE_DIR);
        put(DHCPCD_LEASE_DIR, "x", 1);
        expect("lease dir: a file in the way is refused",
               lease_dir_ready(DHCPCD_LEASE_DIR, u, g) != 0, "accepted a file");
        unlink(DHCPCD_LEASE_DIR);
    }

    return failures != 0;
}
