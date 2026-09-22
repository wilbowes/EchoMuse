#!/usr/bin/env python3
"""Drive an emOS USB serial console from the dev box.

    emos/tools/emconsole.py --list
    emos/tools/emconsole.py --password <pw> <serial> 'uname -a' 'ls /sbin'

Options go BEFORE the port or serial; everything after it is sent verbatim.

THIS FILE EXISTS SO NOBODY WRITES IT AGAIN. It had been rewritten into /tmp
once per session for weeks, which is why the notes about it kept saying
"session-local, rewrite as needed" — a helper that is recreated every time is
one whose traps get rediscovered every time.

Two of those traps are baked in here and must not be simplified away:

  * ECHO OFF FIRST. A port opened with default termios echoes everything the
    device sends back into its own input; the shell then executes its own
    prompt and every command returns 127. It looks alive, echoes what you
    type, and runs nothing.

  * THE COMPLETION MARKER IS ASSEMBLED ON THE DEVICE. Sending
    `cmd; echo __EMxxx__` puts the marker in the shell's echo BEFORE the
    command runs, so the search matches instantly and run() returns the text
    of its own request. `uname -a` "answers" with `uname -a; echo `, and the
    next check then reads the previous command's output.

Both are the same traps `_EmosConsole` in controller/static/dashboard.jsx
carries; this is that class in Python, and they should stay in step.

The device nodes do not survive a host reboot: the container sees the consoles
in /sys but gets no /dev entries, so create them with

    mknod /dev/ttyACM<minor> c 166 <minor>

reading each minor from /sys/class/tty/ttyACM*/dev — it moves between
re-enumerations. --list does this for you.
"""
import argparse
import os
import random
import string
import sys
import termios
import time
import tty


class Console:
    def __init__(self, path, password=None, debug=False):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.debug = debug
        attrs = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.buf = ""
        self.login(password)
        self.send("stty -echo")
        time.sleep(0.4)
        self.drain()

    def drain(self, secs=0.2):
        end = time.time() + secs
        while time.time() < end:
            try:
                b = os.read(self.fd, 4096)
                if b:
                    self.buf += b.decode("utf-8", "replace")
                    end = time.time() + secs
            except BlockingIOError:
                time.sleep(0.02)
        out, self.buf = self.buf, ""
        return out

    def send(self, line):
        os.write(self.fd, (line + "\n").encode())

    def login(self, password):
        """Wake the console and answer the password gate if there is one.

        Waits for what the console prints rather than for a fixed time: the
        gate re-prompts 2-5s late after three wrong answers, and hashing the
        answer takes a moment on the device. A fixed wait sent the password,
        or `stty -echo`, into the gap and read as a wrong password.
        """
        # A prompt left over from an earlier session would be answered in
        # place of the one our newline produces.
        termios.tcflush(self.fd, termios.TCIFLUSH)
        self.drain(0.3)
        os.write(self.fd, b"\n")
        seen = self._await(("password:", "# "), 15)
        if seen.endswith("password:"):
            if password is None:
                raise SystemExit("console asks for a password: pass --password")
            self.send(password)
            if not self._logged_in(30):
                raise SystemExit("console rejected the password")
        if self.debug:
            print(f"[login] {seen!r}", file=sys.stderr)

    def _logged_in(self, timeout):
        """True once a shell prompt follows the password. A password prompt
        alone is not a refusal: it can be the gate's late answer to an
        earlier line, so only one left unanswered for longer than the gate's
        longest backoff (5s) counts."""
        buf, quiet_since = "", time.time()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                b = os.read(self.fd, 4096)
                if b:
                    buf += b.decode("utf-8", "replace")
                    quiet_since = time.time()
                    if buf.rstrip(" ").endswith("#"):
                        return True
                    continue
            except BlockingIOError:
                pass
            if (buf.rstrip(" ").endswith("password:")
                    and time.time() - quiet_since > 6.5):
                return False
            time.sleep(0.05)
        return False

    def _await(self, needles, timeout):
        """Read until the text ends with one of `needles` (trailing blanks
        ignored); return it, or raise if nothing matched in time."""
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                b = os.read(self.fd, 4096)
                if b:
                    buf += b.decode("utf-8", "replace")
                    tail = buf.rstrip(" ")
                    for n in needles:
                        if tail.endswith(n.rstrip(" ")):
                            return tail
                    continue
            except BlockingIOError:
                pass
            time.sleep(0.05)
        raise TimeoutError(f"console did not answer; buffer={buf[-300:]!r}")

    def run(self, cmd, timeout=20):
        tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        self.buf = ""
        # Split so neither marker can appear in the shell's echo of this line.
        self.send(f'__a=__EM; __b={tag}S__; echo "$__a$__b"; {cmd}; '
                  f'__b={tag}__; echo "$__a$__b"')
        start, end = f"__EM{tag}S__", f"__EM{tag}__"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                b = os.read(self.fd, 4096)
                if b:
                    self.buf += b.decode("utf-8", "replace")
            except BlockingIOError:
                time.sleep(0.05)
            i = self.buf.find(end)
            if i >= 0:
                out = self.buf[:i]
                j = out.rfind(start)
                if j >= 0:
                    out = out[j + len(start):]
                return out.lstrip("\r\n")
        raise TimeoutError(f"no answer to {cmd!r}; buffer={self.buf[-300:]!r}")

    def expect(self, needle, timeout=60):
        """Read until `needle` appears; return everything up to and with it.

        For the interactive tools -- em-wifi asks for a menu number and then a
        password, so it cannot be driven by run(), which waits for a marker
        the shell only prints once the command has finished.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                b = os.read(self.fd, 4096)
                if b:
                    self.buf += b.decode("utf-8", "replace")
            except BlockingIOError:
                time.sleep(0.05)
            i = self.buf.find(needle)
            if i >= 0:
                out = self.buf[:i + len(needle)]
                self.buf = self.buf[i + len(needle):]
                return out
        raise TimeoutError(
            f"never saw {needle!r}; buffer={self.buf[-400:]!r}")

    def close(self):
        os.close(self.fd)


def list_consoles(make_nodes=True):
    """Every attached console, by SERIAL — which is the device's identity.

    The minor moves between re-enumerations, so ttyACM0 is not a given device
    and tracking one by port number is how two units get confused for each
    other (see the Lounge/Office swap experiment).
    """
    import glob
    found = []
    for d in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        name = os.path.basename(d)
        try:
            major, minor = open(f"{d}/dev").read().strip().split(":")
        except OSError:
            continue
        serial = ""
        try:
            serial = open(f"{d}/device/../serial").read().strip()
        except OSError:
            pass
        path = f"/dev/{name}"
        if make_nodes and not os.path.exists(path):
            os.system(f"mknod {path} c {major} {minor}")
        found.append((path, serial))
    return found


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Drive an emOS USB serial console.")
    ap.add_argument("port", nargs="?",
                    help="/dev/ttyACM*, or a device serial to look up")
    # REMAINDER, so a command containing spaces, pipes, quotes or a leading
    # dash reaches the device verbatim instead of being parsed as options.
    # Put every --option BEFORE the port.
    ap.add_argument("cmds", nargs=argparse.REMAINDER)
    ap.add_argument("--password", default=None,
                    help="console password, if one is set")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--list", action="store_true",
                    help="list attached consoles by serial, creating the "
                         "/dev nodes if they are missing")
    args = ap.parse_args()

    if args.list or not args.port:
        for path, serial in list_consoles():
            print(f"{path}\t{serial or '(no serial)'}")
        raise SystemExit(0)

    port = args.port
    if not port.startswith("/dev/"):
        # Addressed by serial: the identity that does not move.
        match = [p for p, s in list_consoles() if s == port]
        if not match:
            raise SystemExit(f"no attached console with serial {port!r}")
        port = match[0]

    con = Console(port, password=args.password)
    try:
        for c in args.cmds:
            print(f"\n$ {c}")
            print(con.run(c, timeout=args.timeout).rstrip())
    finally:
        con.close()
