/* Prove init.c's SSH service opens only when the owner wants it, serves what
 * it should, and closes everything when they stop.
 *
 * Each of these is silent on hardware:
 *   - listening with no key on record is an open port nobody can use;
 *   - copying anything but a whole ssh-ed25519 line hands tinysshd input it
 *     was never meant to parse;
 *   - turning SSH off that leaves port 22 open in the filter, or a session
 *     running, is the owner believing they closed something they did not.
 *
 * init.c is included whole and ssh_main() runs for real, against a stub
 * tinysshd (a shell script that answers with an SSH banner) and a stub shell
 * that records the iptables commands instead of running them.
 *
 *   cc -O2 -o sshcheck sshcheck.c && ./sshcheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define T          "/tmp/emos-sshcheck"
#define LEDDIR     T "/led"
#define SSH_DIR    T "/data"
#define SSH_HOME   T "/home"
#define SSH_PASSWD T "/passwd"
#define SSH_BIN    T "/tinysshd"
#define SSH_SH     T "/sh"
#define SSH_PORT   22922
#define main       init_main_unused

#include "init.c"

#undef main

static int failures;

static void ok(int cond, const char *label)
{
    printf("%s  %s\n", cond ? "ok  " : "FAIL", label);
    if (!cond)
        failures++;
}

static void put(const char *path, const char *body, int mode)
{
    FILE *f = fopen(path, "w");
    if (!f) { perror(path); exit(2); }
    fputs(body, f);
    fclose(f);
    chmod(path, mode);
}

static char *slurp(const char *path)
{
    static char b[16384];
    b[0] = 0;
    FILE *f = fopen(path, "r");
    if (!f)
        return NULL;
    size_t n = fread(b, 1, sizeof b - 1, f);
    b[n] = 0;
    fclose(f);
    return b;
}

/* Connect and read the first line; "" when refused. */
static const char *banner(int *fd_out)
{
    static char line[128];
    line[0] = 0;
    int s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(SSH_PORT),
                             .sin_addr.s_addr = htonl(INADDR_LOOPBACK) };
    if (connect(s, (struct sockaddr *)&a, sizeof a) != 0) {
        close(s);
        return line;
    }
    struct timeval tv = { 3, 0 };
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    ssize_t n = read(s, line, sizeof line - 1);
    line[n > 0 ? n : 0] = 0;
    if (fd_out)
        *fd_out = s;
    else
        close(s);
    return line;
}

/* Poll for a condition for up to `secs` (ssh_main re-reads every 2 s). */
#define WAIT_FOR(cond, secs) \
    do { for (int _i = 0; _i < (secs) * 10 && !(cond); _i++) usleep(100000); } \
    while (0)

#define ED1 "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl one\n"
#define ED2 "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB7oQwV2QqHnz5b9wQd0v9ZbM2yCk1E2d1rQh3Q2pYfX two\n"
#define RSA "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7 rsa\n"

int main(void)
{
    if (system("rm -rf " T) != 0) return 2;
    mkdir(T, 0755);
    mkdir(LEDDIR, 0755);
    mkdir(SSH_DIR, 0755);

    /* The stub tinysshd, compiled because it must dispatch on argv[0] as the
     * real one does (a #! script sees its own path in $0, not argv[0]): as
     * tinysshd-makekey it makes a key file, as tinysshd it answers with a
     * banner and holds the session open. */
    put(T "/stub.c",
        "#include <stdio.h>\n#include <string.h>\n#include <unistd.h>\n"
        "#include <sys/stat.h>\n"
        "int main(int c, char **v) {\n"
        "  if (!strcmp(v[0], \"tinysshd-makekey\")) {\n"
        "    char p[512]; mkdir(v[1], 0700);\n"
        "    snprintf(p, sizeof p, \"%s/ed25519.pk\", v[1]);\n"
        "    FILE *f = fopen(p, \"w\"); fputs(\"pk\\n\", f); fclose(f);\n"
        "    f = fopen(\"" T "/makekey.log\", \"a\"); fputs(\"made\\n\", f);\n"
        "    fclose(f); return 0; }\n"
        "  if (!strcmp(v[0], \"tinysshd\")) {\n"
        "    write(1, \"SSH-2.0-stub\\r\\n\", 14); sleep(30); return 0; }\n"
        "  return 9; }\n", 0644);
    if (system("cc -o " SSH_BIN " " T "/stub.c")) { puts("cannot build stub"); return 2; }
    /* The stub shell: records `-c <cmd>` rather than touching iptables. */
    put(SSH_SH, "#!/bin/sh\nprintf '%s\\n' \"$2\" >> " T "/fw.log\n", 0755);

    /* ── ssh_wanted ── */
    ok(!ssh_wanted(), "no flag, no keys: off");
    put(SSH_DIR "/enabled", "", 0644);
    ok(!ssh_wanted(), "flag but no key file: off");
    put(SSH_DIR "/authorized_keys", RSA, 0644);
    ok(!ssh_wanted(), "flag and only a non-Ed25519 key: off");
    put(SSH_DIR "/authorized_keys", RSA ED1, 0644);
    ok(ssh_wanted(), "flag and an Ed25519 key: on");
    unlink(SSH_DIR "/enabled");
    ok(!ssh_wanted(), "keys but no flag: off");

    /* ── ssh_copy_keys: whole ssh-ed25519 lines only ── */
    put(SSH_DIR "/authorized_keys",
        RSA ED1 "# comment\n" " ssh-ed25519 AAAA leading-space\n" ED2
        "ssh-ed25519 AAAA no-newline", 0644);
    int n = ssh_copy_keys();
    ok(n == 2, "copies the two whole Ed25519 lines and nothing else");
    char *got = slurp(SSH_HOME "/.ssh/authorized_keys");
    ok(got && strcmp(got, ED1 ED2) == 0, "the copy is exactly those lines");
    struct stat st;
    ok(stat(SSH_HOME "/.ssh", &st) == 0 && (st.st_mode & 0777) == 0700,
       ".ssh is 0700");
    ok(stat(SSH_HOME "/.ssh/authorized_keys", &st) == 0
       && (st.st_mode & 0777) == 0600, "authorized_keys is 0600");
    ok(stat(SSH_HOME, &st) == 0 && (st.st_mode & 022) == 0,
       "home is not group- or world-writable (tinysshd refuses that)");
    struct stat before, after;
    stat(SSH_HOME "/.ssh/authorized_keys", &before);
    sleep(1);
    ssh_copy_keys();
    stat(SSH_HOME "/.ssh/authorized_keys", &after);
    ok(before.st_ino == after.st_ino && before.st_mtime == after.st_mtime,
       "an unchanged key list is not rewritten");

    /* ── ssh_passwd ── */
    if (symlink("/nonexistent/passwd", SSH_PASSWD)) {}
    ok(ssh_passwd() == 0, "writes passwd over a symlink");
    got = slurp(SSH_PASSWD);
    ok(got && strcmp(got, "root:x:0:0:root:" SSH_HOME ":" SSH_SH "\n") == 0,
       "passwd names root, the ramdisk home and the shell");
    ok(lstat(SSH_PASSWD, &st) == 0 && S_ISREG(st.st_mode),
       "passwd is a regular file, not the old symlink");

    /* ── ssh_hostkey: made once, by argv[0] ── */
    ok(ssh_hostkey() == 0, "host key made");
    ok(ssh_hostkey() == 0, "host key kept");
    got = slurp(T "/makekey.log");
    ok(got && strcmp(got, "made\n") == 0,
       "tinysshd-makekey ran once, chosen by argv[0]");

    /* ── ssh_main, end to end ── */
    unlink(T "/fw.log");
    unlink(SSH_DIR "/authorized_keys");
    pid_t svc = fork();
    if (svc == 0) {
        ssh_main();
        _exit(0);
    }
    sleep(3);
    ok(banner(NULL)[0] == 0, "off: nothing listens");
    ok(slurp(T "/fw.log") == NULL, "off: the filter is not touched");

    put(SSH_DIR "/authorized_keys", ED1, 0644);
    put(SSH_DIR "/enabled", "", 0644);
    WAIT_FOR(strncmp(banner(NULL), "SSH-2.0-", 8) == 0, 5);
    int live = -1;
    ok(strncmp(banner(&live), "SSH-2.0-stub", 12) == 0,
       "on: the port answers with tinysshd");
    got = slurp(T "/fw.log");
    ok(got && strstr(got, "-I INPUT -p tcp --dport 22922 -j ACCEPT"),
       "on: port opened in the filter");

    unlink(SSH_DIR "/enabled");
    WAIT_FOR(banner(NULL)[0] == 0, 5);
    ok(banner(NULL)[0] == 0, "flag removed: the port closes");
    got = slurp(T "/fw.log");
    ok(got && strstr(got, "-D INPUT -p tcp --dport 22922 -j ACCEPT"),
       "flag removed: port closed in the filter");
    char c;
    struct timeval tv = { 3, 0 };
    setsockopt(live, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    ok(read(live, &c, 1) == 0, "flag removed: the live session is ended");
    close(live);
    ok(access(SSH_HOME "/.ssh/authorized_keys", F_OK) != 0,
       "flag removed: the copied keys are gone");

    put(SSH_DIR "/enabled", "", 0644);
    WAIT_FOR(strncmp(banner(NULL), "SSH-2.0-", 8) == 0, 5);
    ok(strncmp(banner(NULL), "SSH-2.0-stub", 12) == 0, "on again: listens");
    put(SSH_DIR "/authorized_keys", RSA, 0644);
    WAIT_FOR(banner(NULL)[0] == 0, 5);
    ok(banner(NULL)[0] == 0, "last Ed25519 key removed: the port closes");

    kill(svc, SIGKILL);
    waitpid(svc, NULL, 0);
    if (system("rm -rf " T)) {}

    printf("%s\n", failures ? "SSH CHECK FAILED" : "ssh: all checks pass");
    return failures != 0;
}
