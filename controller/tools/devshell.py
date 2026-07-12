"""One-shot device shell command runner via controller shell proxy."""
import asyncio, json, secrets, sqlite3, sys, time
import websockets

DEVICE = "G090LF11803611NF"
DB = "/app/data/echomuse.db"

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

async def run(cmds):
    tok = make_token()
    try:
        uri = f"ws://127.0.0.1:8768/api/devices/{DEVICE}/shell?token={tok}"
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
    asyncio.run(run(sys.argv[1:]))
