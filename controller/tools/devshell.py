"""One-shot device shell command runner via controller shell proxy.

    devshell.py [-d SERIAL]... [--all] "<cmd>" ["<cmd>" ...]

The device used to be a constant at the top of this file, which meant asking
the same question of a second device was a file edit — and in practice meant
writing a throwaway copy of this script instead. Hence -d, and --all for the
whole fleet, which is what most diagnostic questions actually want.

READ ONLY, in practice: a shell opened here is a CHILD OF THE SERVER, so
`stop`ping the service kills this shell at the first word and takes the device
down until it is power cycled. Inspect freely; do not stop the server.
"""
import asyncio, json, secrets, sqlite3, sys, time
import websockets

# Default when neither -d nor --all is given — kept so existing invocations
# and the README's one-liner behave exactly as before.
DEVICE = "G090LF11803611NF"

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



def all_devices(max_age_s=300):
    """Devices seen recently — i.e. ones a shell can actually reach.

    Filtered rather than "every row" because the shell proxy has no fast
    failure for an absent device: it simply waits, so one retired device turns
    a fleet-wide question into a minute of nothing. Name an offline device
    with -d if you really want to wait for it.
    """
    con = sqlite3.connect(DB)
    cutoff = time.time() - max_age_s
    rows = [r[0] for r in con.execute(
        "SELECT device_id FROM devices WHERE last_seen > ? ORDER BY last_seen DESC",
        (cutoff,))]
    con.close()
    return rows


def parse_args(argv):
    """Returns (devices, commands). Anything not a flag is a command."""
    devices, cmds, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("-d", "--device"):
            i += 1
            if i >= len(argv):
                raise SystemExit("-d needs a device serial")
            devices.append(argv[i])
        elif a == "--all":
            devices.extend(all_devices())
        else:
            cmds.append(a)
        i += 1
    return (devices or [DEVICE]), cmds

def make_token():
    tok = secrets.token_hex(32)
    con = sqlite3.connect(DB)
    # mirror schema: inspect columns
    cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
    print("sessions cols:", cols, file=sys.stderr)
    if "expires_at" in cols:
        con.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, 1, datetime('now'), datetime('now', '+10 minutes'))", (tok,))
    else:
        con.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, 1, datetime('now'))", (tok,))
    con.commit(); con.close()
    return tok

def drop_token(tok):
    con = sqlite3.connect(DB)
    con.execute("DELETE FROM sessions WHERE token=?", (tok,))
    con.commit(); con.close()

async def run(cmds, device=DEVICE):
    tok = make_token()
    try:
        uri = f"ws://127.0.0.1:8768/api/devices/{device}/shell?token={tok}"
        async with websockets.connect(uri, max_size=None) as ws:
            out = bytearray()
            async def read_until(marker, timeout=15):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
                    except asyncio.TimeoutError:
                        break
                    if isinstance(msg, str):
                        print("META:", msg, file=sys.stderr); continue
                    out.extend(msg)
                    if marker in bytes(out[-200:]):
                        return
            await read_until(b"# ")
            for c in cmds:
                out.clear()
                await ws.send(b"\x00" + (c + "\n").encode())
                await read_until(b"# ", timeout=20)
                print(f"$ {c}\n{bytes(out).decode('utf-8', 'replace')}\n{'-'*60}")
    finally:
        drop_token(tok)

if __name__ == "__main__":
    devices, commands = parse_args(sys.argv[1:])
    if not commands:
        raise SystemExit(__doc__)
    for dev in devices:
        if len(devices) > 1:
            print(f"\n═══ {dev}")
        asyncio.run(run(commands, dev))
