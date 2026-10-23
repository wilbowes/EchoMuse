/* Prove service control reaches init and does what it says (#560).
 *
 * `emos-svc stop echomuse` reaching a service that is then quietly respawned
 * is exactly the silent-on-hardware failure the other off-target checks exist
 * for: the tool reports success, the ring keeps going, and nothing says the
 * stop did not take. The reverse is worse — a hold that is never released —
 * because on a device with no adb a maintenance window becomes a reflash.
 *
 * The three things that would have failed on first use in the first attempt at
 * this (#560, closed):
 *
 *   - the tool cannot reach init unless INIT HOLDS THE FIFO OPEN. The first
 *     attempt opened it, read and closed it once a second while the tool opens
 *     O_WRONLY | O_NONBLOCK, so with no reader holding it the open failed
 *     ENXIO and nearly every call printed "no supervisor listening". The
 *     protocol checks below run the tool in a child that does NOT inherit
 *     init's end, so they only pass while the parent is really holding it.
 *   - `restart` must clear the hold, or it is a slow `stop`.
 *   - `status` must be answered to the caller, not printed in PID 1 where
 *     nobody sees it.
 *
 * The tool runs as a child process and the supervisor's own poll runs in the
 * parent, over a real FIFO, so this drives the request line, the hold and the
 * reply the device runs rather than a description of them. The service that a
 * stop kills is a real child of this process: a made-up pid would either
 * signal an unrelated process on this machine or prove nothing.
 *
 * init.c is included whole, as ringsim.c, pwcheck.c and trialcheck.c do.
 *
 *   cc -O2 -o svccheck svccheck.c && ./svccheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* The FIFOs in /run belong to the device; this check must not touch them. */
#define SVC_FIFO       "/tmp/emos-svccheck.ctl"
#define SVC_REPLY_FIFO "/tmp/emos-svccheck.%d"
#define main   init_main_unused

#include "init.c"

#undef main

static int failures;

static void ok(const char *what)
{
    printf("  ok    %s\n", what);
}

static void bad(const char *what, const char *why)
{
    failures++;
    printf("  FAIL  %s: %s\n", what, why);
}

static int is(const char *what, int got, int want)
{
    if (got == want)
        return 1;
    char buf[160];
    snprintf(buf, sizeof buf, "got %d, want %d", got, want);
    bad(what, buf);
    return 0;
}

static int has(const char *what, const char *hay, const char *needle)
{
    if (strstr(hay, needle))
        return 1;
    char buf[600];
    snprintf(buf, sizeof buf, "\"%s\" not in \"%s\"", needle, hay);
    bad(what, buf);
    return 0;
}

/* A table init can reason about, without running anything. */
static void fake_services(void)
{
    nsvc = 0;
    svc_add("echomuse", NULL, "/data/local/bin/start_server.sh", NULL);
    svc_add("console", NULL, "/system/bin/sh", NULL);
    for (int i = 0; i < nsvc; i++) {
        svcs[i].pid = -1;
        svcs[i].started = 0;
        svcs[i].fails = 0;
        svcs[i].gone = 0;
        svcs[i].held = 0;
        svcs[i].stopping = 0;
    }
}

/* A service that is really there, so that the SIGTERM a stop sends has
 * something to land on and the check can see that it died. */
static pid_t live_service(void)
{
    pid_t pid = fork();
    if (pid == 0) {
        for (;;)
            pause();
        _exit(0);
    }
    return pid;
}

static int service_gone(pid_t pid, int wait_ms)
{
    int st;
    for (int waited = 0; waited <= wait_ms; waited += 20) {
        if (waitpid(pid, &st, WNOHANG) == pid)
            return 1;
        usleep(20000);
    }
    return 0;
}

/*
 * Run the real tool — dispatch, request line, FIFO, answer — in a child, and
 * run the supervisor's own poll in the parent while it waits, which is the
 * device's arrangement with the second between passes removed. Returns the
 * child's exit status and copies what it printed into `out`.
 */
static int run_tool(char **argv, char *out, size_t cap)
{
    int p[2], argc = 0, st = 0, n = 0;
    pid_t pid;
    ssize_t k;

    while (argv[argc])
        argc++;
    if (pipe(p) != 0) {
        bad("pipe", strerror(errno));
        return -1;
    }
    pid = fork();
    if (pid == 0) {
        dup2(p[1], 1);
        dup2(p[1], 2);
        close(p[0]);
        close(p[1]);
        /* The child must NOT hold init's end of the control FIFO: the tool's
         * open only succeeds because the supervisor has it open, and that is
         * the property these checks are for. */
        if (svc_fd >= 0)
            close(svc_fd);
        svc_fd = -1;
        _exit(tool_main(argc, argv));
    }
    close(p[1]);

    for (;;) {
        svc_poll();
        if (waitpid(pid, &st, WNOHANG) == pid)
            break;
        usleep(20000);
    }
    while (n < (int)cap - 1 && (k = read(p[0], out + n, cap - 1 - n)) > 0)
        n += k;
    out[n] = '\0';
    close(p[0]);
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

int main(void)
{
    struct svc_req r;
    char out[2048];
    char *args[4];
    pid_t svc;

    printf("request parsing:\n");
    if (is("stop parses", svc_parse("stop", &r), 0))
        ok("stop parses");
    is("stop is the stop verb", r.op == SVC_OP_STOP, 1);
    if (is("a bare verb names every service", r.name[0], '\0'))
        ok("a bare verb means every service");
    if (is("start parses", svc_parse("start echomuse", &r), 0) &&
        is("start is the start verb", r.op == SVC_OP_START, 1))
        ok("start parses");
    if (is("the name survives parsing", strcmp(r.name, "echomuse"), 0))
        ok("the name is carried");
    if (is("restart parses", svc_parse("restart", &r), 0) &&
        is("restart is the restart verb", r.op == SVC_OP_RESTART, 1))
        ok("restart parses");
    if (is("status parses", svc_parse("status", &r), 0) &&
        is("status is the status verb", r.op == SVC_OP_STATUS, 1))
        ok("status parses");
    /* The tool spells "every service" as -, because sscanf's field splitting
     * cannot see an empty field. */
    if (is("a dash is every service", svc_parse("stop -", &r), 0) &&
        is("and names none", r.name[0], '\0'))
        ok("a dash names every service");
    if (is("the reply pid is carried", svc_parse("status - 4242", &r), 0) &&
        is("the reply pid is right", r.reply, 4242) &&
        is("and the name is still none", r.name[0], '\0'))
        ok("the caller's pid travels with the request");
    /* THE trap: an unknown verb must be refused, not treated as "all" and not
     * passed on as a boot mode. Before #560 `/init stop echomuse` rebooted the
     * device. */
    is("unknown verb refused", svc_parse("reboot", &r), -1);
    is("empty line refused", svc_parse("", &r), -1);
    is("whitespace refused", svc_parse("   ", &r), -1);
    is("NULL refused", svc_parse(NULL, &r), -1);
    ok("only the four verbs are accepted");
    /* The name is checked against the table, which the tool cannot see. A name
     * that matches nothing is a mistake, never "every service". */
    fake_services();
    is("a known name is known", svc_known("echomuse"), 1);
    is("no name is every service", svc_known(""), 1);
    is("an unknown name is not", svc_known("echomuse2"), 0);
    ok("a name that matches nothing is refused, not applied to all");

    printf("boot modes (/init <word>):\n");
    is("recovery is a known boot mode", is_boot_mode("recovery"), 1);
    /* Before #560 ANY word reached reboot_into, so a mistyped one rebooted. */
    is("stop is not a boot mode", is_boot_mode("stop"), 0);
    is("a typo is not a boot mode", is_boot_mode("recoveru"), 0);
    is("empty is not a boot mode", is_boot_mode(""), 0);
    ok("only a known mode reaches reboot_into");

    /* The other half of the same trap: the service verbs arrive under the name
     * `emos-svc`, so an /init invocation cannot reach them at all. */
    args[0] = (char *)"/init";
    args[1] = (char *)"stop";
    args[2] = NULL;
    if (is("/init stop is refused", run_tool(args, out, sizeof out), 1))
        ok("an /init word that is not a boot mode does not reboot");
    has("/init stop says why", out, "not a boot mode");
    args[1] = (char *)"--help";
    if (is("/init --help still works", run_tool(args, out, sizeof out), 0))
        ok("/init --help is unchanged");

    printf("the protocol, over a real FIFO:\n");
    fake_services();
    svc_open_control();
    if (is("init holds the control FIFO open", svc_fd >= 0, 1))
        ok("init has its end of the FIFO open");

    /* status: answered to the caller. PID 1 printing it was the third defect,
     * so what is checked is what the PERSON who typed the command sees. */
    svcs[0].pid = live_service();
    /* Told it just started, so an exit read as a crash would show: without the
     * `stopping` mark this one is a fast exit and earns a backoff. */
    svcs[0].started = time(NULL);
    args[0] = (char *)"emos-svc";
    args[1] = (char *)"status";
    args[2] = NULL;
    if (is("status exits 0", run_tool(args, out, sizeof out), 0))
        ok("status answers with a success");
    has("status lists the first service", out, "echomuse");
    has("status lists the second service", out, "console");
    has("status says it is running", out, "running");
    {
        char pid[32];
        snprintf(pid, sizeof pid, "pid=%d", (int)svcs[0].pid);
        has("status carries the pid", out, pid);
    }
    has("status carries the fail count", out, "fails=");
    has("status carries the backoff", out, "backoff=");

    /* A name filters the listing, the way it filters a stop. Nobody reads a
     * two-service dump to find out about one, and the console's line beside
     * echomuse's reads as echomuse's. */
    args[1] = (char *)"status";
    args[2] = (char *)"echomuse";
    args[3] = NULL;
    run_tool(args, out, sizeof out);
    has("status with a name reports that service", out, "echomuse");
    if (strstr(out, "console"))
        bad("status with a name lists only it", "the console's line came too");
    else
        ok("a name narrows the status listing");

    /* stop: the hold is set, the process is signalled, and the supervisor's
     * own predicate refuses to start it again. */
    args[1] = (char *)"stop";
    args[2] = (char *)"echomuse";
    args[3] = NULL;
    svc = svcs[0].pid;
    if (is("stop exits 0", run_tool(args, out, sizeof out), 0))
        ok("stop answers with a success");
    has("stop names what it held", out, "echomuse held");
    is("the named service is held", svcs[0].held, 1);
    is("the neighbour is untouched", svcs[1].held, 0);
    if (is("SIGTERM reached it", service_gone(svc, 2000), 1))
        ok("the service was really signalled");
    /* The reaper's own decision, not a restatement of it. */
    svc_exit(&svcs[0], time(NULL));
    is("a stop is not counted as a crash", svcs[0].fails, 0);
    is("the pid is released when it is really gone", svcs[0].pid, -1);
    /* The supervisor's own predicate, not a restatement of it. Asking
     * svc_should_start is the only version of this that can fail: a check that
     * restates the condition in its own words keeps passing when the condition
     * is removed from supervise(), which is the failure it exists to catch — a
     * stop the tool reported as applied, and a respawn a second later. */
    if (is("a held service is not a respawn candidate",
           svc_should_start(&svcs[0], time(NULL)), 0))
        ok("the supervisor's own predicate skips a held service");

    args[1] = (char *)"status";
    args[2] = NULL;
    run_tool(args, out, sizeof out);
    has("status says a held service is held", out, "held");

    /* start: the hold is cleared, and the service is a candidate again. */
    args[1] = (char *)"start";
    args[2] = (char *)"echomuse";
    args[3] = NULL;
    if (is("start exits 0", run_tool(args, out, sizeof out), 0))
        ok("start answers with a success");
    has("start names what it released", out, "echomuse released");
    is("start clears the hold", svcs[0].held, 0);
    if (is("a released service is a start candidate",
           svc_should_start(&svcs[0], time(NULL)), 1))
        ok("start puts it back in the running set");

    /* A service marked absent at boot (`req` missing) is asked for again by
     * hand: without clearing `gone` the tool answers "released" while the
     * supervisor goes on skipping it, which is a success nothing acted on. */
    svcs[0].gone = 1;
    args[1] = (char *)"start";
    args[2] = (char *)"echomuse";
    args[3] = NULL;
    run_tool(args, out, sizeof out);
    if (is("start reaches a service marked absent", svcs[0].gone, 0))
        ok("start re-asks for a service the boot marked absent");

    /* restart. Defect #2: it shared the stop path, set `held`, and nothing
     * cleared it, so the service stayed down until someone typed `start`. */
    svcs[0].pid = live_service();
    svc = svcs[0].pid;
    args[1] = (char *)"restart";
    if (is("restart exits 0", run_tool(args, out, sizeof out), 0))
        ok("restart answers with a success");
    has("restart says what it is doing", out, "echomuse restarting");
    is("restart does NOT hold the service", svcs[0].held, 0);
    if (is("restart signals the running service", service_gone(svc, 2000), 1))
        ok("restart stops the process that is up");
    /* A service that died the instant it was signalled: without the stopping
     * mark this is a fast exit and earns a backoff. */
    svcs[0].started = time(NULL);
    svc_exit(&svcs[0], time(NULL));
    is("a restart is not counted as a crash either", svcs[0].fails, 0);
    /* Back to where svc_apply left it — the decision below is about the hold,
     * not about the clock. */
    svcs[0].started = 0;
    if (is("and the supervisor starts it again",
           svc_should_start(&svcs[0], time(NULL)), 1))
        ok("restart clears the hold and lets it be respawned");

    /* A bare verb covers every service. */
    svcs[0].pid = live_service();
    svcs[1].pid = live_service();
    args[1] = (char *)"stop";
    args[2] = NULL;
    args[3] = NULL;
    if (is("a bare stop exits 0", run_tool(args, out, sizeof out), 0))
        ok("a bare verb reaches init");
    has("a bare stop names every service", out, "every service held");
    is("both services are held", svcs[0].held + svcs[1].held, 2);
    ok("a bare verb holds everything");
    service_gone(svcs[0].pid, 2000);
    service_gone(svcs[1].pid, 2000);

    /* Two requests that arrive together in one read are two requests. Acting
     * on only the first drops the second caller into the five-second answer
     * timeout for a change that never happened, so BOTH lines go in one write
     * and both must land. The replies name no pid, which is also the path a
     * request typed by hand at the console takes: init answers it on its own
     * stdout, and the two lines below are that answer. */
    printf("two requests, one read:\n");
    fflush(stdout);               /* before svc_poll writes init's answers */
    fake_services();
    {
        static const char two[] = "stop echomuse 0\nstop console 0\n";
        int ctl = open(SVC_FIFO, O_WRONLY | O_NONBLOCK);
        if (ctl < 0)
            bad("two requests in one read", strerror(errno));
        else {
            if (write(ctl, two, sizeof two - 1) != (ssize_t)(sizeof two - 1))
                bad("two requests in one read", strerror(errno));
            close(ctl);
            svc_poll();
            if (is("both lines in one read are acted on",
                   svcs[0].held + svcs[1].held, 2))
                ok("a second request is not dropped by the first");
        }
    }

    /* Refusals, which the caller has to be told about: silently doing nothing
     * is how a typo becomes an afternoon. */
    args[1] = (char *)"stop";
    args[2] = (char *)"nosuch";
    args[3] = NULL;
    if (is("an unknown name fails", run_tool(args, out, sizeof out), 1))
        ok("a name that matches nothing is a failure");
    has("and says why", out, "no service named \"nosuch\"");
    args[1] = (char *)"reboot";
    args[2] = NULL;
    args[3] = NULL;
    if (is("an unknown verb fails", run_tool(args, out, sizeof out), 1))
        ok("an unknown verb is refused at the FIFO too");
    has("and says so", out, "not a request: \"reboot\"");
    /* Refused before anything is created, or before it can reach init as a
     * different request than the one that was typed. */
    args[1] = (char *)"stop";
    args[2] = (char *)"";
    args[3] = NULL;
    if (is("an empty name is refused", run_tool(args, out, sizeof out), 2))
        ok("an empty name is refused rather than read as the reply pid");
    has("and says which field", out, "empty service name");
    {
        static char longname[256];
        memset(longname, 'x', sizeof longname - 1);
        longname[sizeof longname - 1] = '\0';
        args[2] = longname;
        if (is("an over-long request is refused",
               run_tool(args, out, sizeof out), 2))
            ok("a request too long for the line buffer is refused");
    }

    /* And the failure the first attempt shipped in place of the feature: with
     * nothing holding the FIFO, the tool must say so rather than hang. */
    close(svc_fd);
    svc_fd = -1;
    args[1] = (char *)"status";
    args[2] = NULL;
    if (is("no supervisor is an error", run_tool(args, out, sizeof out), 1))
        ok("a call with no supervisor fails instead of hanging");
    has("and names the FIFO it could not open", out, "no emOS supervisor");

    if (failures) {
        printf("\n%d FAILED\n", failures);
        return 1;
    }
    printf("\nsvccheck: all service-control checks passed\n");
    return 0;
}
