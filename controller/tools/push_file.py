"""Push a local file to a device over the controller shell proxy, resumably.

    docker cp controller/tools/push_file.py echomuse-controller:/tmp/
    docker cp device/build/oww_probe echomuse-controller:/tmp/
    docker exec echomuse-controller python /tmp/push_file.py \
        <device_id> /tmp/oww_probe /data/local/tmp/oww_probe [--chmod 755]

Why this exists rather than `ota.py`: that pushes the firmware binary through
the update endpoint, which writes one specific place. This puts an arbitrary
file anywhere, which is what the on-device wake word work needs (a 12.3MB
libonnxruntime.so, three .onnx models, a test fixture).

Five device-specific traps are baked in, each of which produced a convincing
wrong answer first:

  * The shell is a PTY in canonical mode, so a single command line longer than
    ~4096 bytes is mangled by the line discipline — silently, and the mangling
    looks like a corrupt transfer rather than a too-long line. Data therefore
    goes as SHORT base64 lines (76 chars, from encodebytes) inside a heredoc,
    which is what `em_api._stream_file_to_device` does to move 10MB firmware
    reliably. One heredoc per BLOCK_BYTES rather than one for the whole file,
    so an interrupted transfer has somewhere to resume from.

  * The shell plane drops after roughly 50 seconds of sustained load, so the
    transfer reconnects between blocks and picks up where it left off. This is
    not optional for anything multi-megabyte.

  * Resuming on file SIZE will happily append to a *different* file that
    happens to be there, and then report success while the device runs stale
    bytes. So the default is to delete the destination first; --resume is
    opt-in, and even then the md5 check at the end is what decides.

  * `busybox truncate` silently no-ops on this build. Trimming a partial tail
    uses `dd` + `mv` instead.

  * The PTY echoes each command back, so a completion marker sent as a literal
    string matches its OWN echo — that is how an empty file once reported
    "transfer OK". Markers here are split in the source (M'K'1) so the echoed
    text differs from what is matched. The data path avoids markers entirely by
    waiting for the shell prompt, which is safe because base64 contains no '#'.

Success is md5 agreement, not "no errors". Exit status is non-zero otherwise.
"""
import asyncio, base64, hashlib, os, secrets, sqlite3, sys, time
import websockets

def _resolve_db() -> str:
    # #270: the container keeps its database at /app/data, the HA add-on at
    # /data. EM_DB overrides both. Inlined rather than shared: these tools
    # are docker-cp'd into the container one file at a time and must stay
    # standalone.
    import os
    env = os.environ.get("EM_DB", "").strip()
    if env:
        return env
    for c in ("/data/echomuse.db", "/app/data/echomuse.db"):
        if os.path.exists(c):
            return c
    return "/app/data/echomuse.db"


DB = _resolve_db()

# Bytes per heredoc block. A multiple of 3 so each block's base64 is padding
# free — padding in the middle of the file would decode to garbage. Sized as a
# compromise: bigger means fewer round trips, smaller means less to redo when a
# block fails and a finer resume granularity.
BLOCK_BYTES = 98304  # 3 * 32768
# Reconnect at least this often. The plane dies somewhere around 50s under
# load, so rotate well before that rather than discovering the edge.
RECONNECT_S = 25
# Attempts per block before giving up (first try plus retries).
BLOCK_TRIES = 4
# Not in the base64 alphabet, so it cannot appear in the data.
DELIM = "__END_B64_42__"


def make_token():
    tok = secrets.token_hex(32)
    con = sqlite3.connect(DB)
    cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
    if "expires_at" in cols:
        con.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, 1, datetime('now'), datetime('now', '+30 minutes'))", (tok,))
    else:
        con.execute("INSERT INTO sessions (token, user_id, created_at) "
                    "VALUES (?, 1, datetime('now'))", (tok,))
    con.commit()
    con.close()
    return tok


def drop_token(tok):
    con = sqlite3.connect(DB)
    con.execute("DELETE FROM sessions WHERE token=?", (tok,))
    con.commit()
    con.close()


class Shell:
    """One shell session. Short-lived by design — see RECONNECT_S."""

    def __init__(self, ws):
        self.ws = ws
        self.buf = bytearray()

    async def read_until(self, marker, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            if isinstance(msg, str):
                continue
            self.buf.extend(msg)
            if marker in bytes(self.buf[-4096:]):
                return True
        return False

    async def send(self, text):
        await self.ws.send(b"\x00" + text.encode())

    async def cmd(self, line, timeout=30):
        """Run a command and return its output, prompt-delimited."""
        self.buf.clear()
        await self.send(line + "\n")
        ok = await self.read_until(b"# ", timeout=timeout)
        if not ok:
            raise RuntimeError(f"timed out waiting for prompt after: {line[:60]}")
        return bytes(self.buf).decode("utf-8", "replace")

    async def append_block(self, dest, blk):
        """Append one block to dest as base64 lines inside a heredoc.

        Every line is short (76 chars) because the PTY's canonical-mode line
        limit mangles anything near 4096 — the failure that made a chunk-per-
        command version of this write nothing at all while looking fine.
        """
        self.buf.clear()
        await self.send(f"busybox base64 -d << '{DELIM}' >> {dest}\n")
        for line in base64.encodebytes(blk).decode("ascii").splitlines(keepends=True):
            await self.send(line)
        await self.send(f"{DELIM}\n")
        if not await self.read_until(b"# ", timeout=120):
            raise RuntimeError("timed out waiting for prompt after a heredoc block")


async def connect(device, tok):
    uri = f"ws://127.0.0.1:8768/api/devices/{device}/shell?token={tok}"
    ws = await websockets.connect(uri, max_size=None)
    sh = Shell(ws)
    await sh.read_until(b"# ")
    # Suppress echo where the device supports it; the code never relies on
    # that working, only benefits from the smaller reads.
    await sh.cmd("busybox stty -echo 2>/dev/null", timeout=10)
    return ws, sh


async def close_quietly(ws):
    try:
        await ws.close()
    except Exception:
        pass


async def trim_to(sh, path, size):
    """Cut path back to exactly `size` bytes.

    Uses dd + mv because `busybox truncate` silently does nothing on this
    build — a no-op there would leave the partial tail in place and corrupt
    everything appended after it, while every step still reported success.
    """
    if size == 0:
        await sh.cmd(f"rm -f {path}")
        return
    await sh.cmd(f"busybox dd if={path} of={path}.t bs={size} count=1 2>/dev/null && "
                 f"busybox mv {path}.t {path}")
    got = await remote_size(sh, path)
    if got != size:
        raise RuntimeError(f"trim to {size} left {got} bytes")


async def remote_size(sh, path):
    out = await sh.cmd(f"busybox wc -c < {path} 2>/dev/null || echo 0")
    for tok in reversed(out.split()):
        if tok.isdigit():
            return int(tok)
    return 0


async def remote_md5(sh, path):
    # Split marker: the PTY echoes this command back, and an unsplit marker
    # would match its own echo.
    out = await sh.cmd(f"echo M'D'5:$(busybox md5sum {path} | busybox cut -d' ' -f1)", timeout=120)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("MD5:") and len(line) == 4 + 32:
            return line[4:]
    raise RuntimeError(f"could not read md5 of {path}; got: {out[-200:]!r}")


async def push(device, local, remote, mode, resume):
    data = open(local, "rb").read()
    want_md5 = hashlib.md5(data).hexdigest()
    payload = BLOCK_BYTES
    print(f"{local} -> {device}:{remote}", file=sys.stderr)
    print(f"{len(data)} bytes, md5 {want_md5}, {payload}B/block", file=sys.stderr)

    tok = make_token()
    sent = 0
    try:
        ws, sh = await connect(device, tok)
        try:
            await sh.cmd(f"mkdir -p {os.path.dirname(remote) or '/'}")
            if resume:
                have = await remote_size(sh, remote)
                # Only resume on a whole-chunk boundary; trim the partial tail
                # with dd, because truncate silently does nothing here.
                sent = have - (have % payload)
                if have != sent:
                    await sh.cmd(f"busybox dd if={remote} of={remote}.t bs={sent} count=1 "
                                 f"2>/dev/null && busybox mv {remote}.t {remote}")
                if sent:
                    print(f"resuming at {sent} bytes", file=sys.stderr)
            else:
                # The default. Appending to whatever was there is how a stale
                # binary gets run while the push reports success.
                await sh.cmd(f"rm -f {remote}")

            opened = time.monotonic()
            while sent < len(data):
                if time.monotonic() - opened > RECONNECT_S:
                    await close_quietly(ws)
                    ws, sh = await connect(device, tok)
                    opened = time.monotonic()

                blk = data[sent:sent + payload]
                # The plane can drop mid-block, not only between blocks, so
                # each block gets its own retry. Recovery trims the file back
                # to this block's start offset rather than trusting whatever
                # partial decode landed: a truncated base64 line can decode to
                # a few bytes that are not a prefix of anything.
                for attempt in range(1, BLOCK_TRIES + 1):
                    try:
                        # No marker needed: base64 contains no '#', so waiting
                        # for the prompt cannot match the echoed heredoc.
                        await sh.append_block(remote, blk)
                        break
                    except Exception as e:
                        if attempt == BLOCK_TRIES:
                            raise
                        print(f"\n  block at {sent} failed ({type(e).__name__}: {e}); "
                              f"retry {attempt}/{BLOCK_TRIES - 1}", file=sys.stderr)
                        await close_quietly(ws)
                        ws, sh = await connect(device, tok)
                        opened = time.monotonic()
                        await trim_to(sh, remote, sent)
                sent += len(blk)
                pct = 100.0 * sent / len(data)
                print(f"\r  {sent}/{len(data)} bytes ({pct:.1f}%)", end="", file=sys.stderr)
            print(file=sys.stderr)

            got = await remote_md5(sh, remote)
            if got != want_md5:
                print(f"MD5 MISMATCH: device has {got}, local is {want_md5}", file=sys.stderr)
                return 1
            print(f"md5 ok: {got}", file=sys.stderr)
            if mode:
                await sh.cmd(f"chmod {mode} {remote}")
                print(f"chmod {mode}", file=sys.stderr)
            return 0
        finally:
            await ws.close()
    finally:
        drop_token(tok)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device", help="device id, e.g. G090LF11803611NF")
    ap.add_argument("local", help="local path (inside the controller container)")
    ap.add_argument("remote", help="destination path on the device")
    ap.add_argument("--chmod", metavar="MODE", help="chmod the file after a verified push")
    ap.add_argument("--resume", action="store_true",
                    help="continue an interrupted push instead of deleting the destination; "
                         "the md5 check still decides")
    a = ap.parse_args()
    sys.exit(asyncio.run(push(a.device, a.local, a.remote, a.chmod, a.resume)))
