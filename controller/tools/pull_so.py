import asyncio, secrets, sqlite3, sys, time, base64, re
import websockets

DEVICE = "G090LF11803611NF"; DB = "/app/data/echomuse.db"
SRC = sys.argv[1]

def tok_add():
    t = secrets.token_hex(32); c = sqlite3.connect(DB)
    c.execute("INSERT INTO sessions (token,user_id,created_at,expires_at) VALUES (?,1,datetime('now'),datetime('now','+10 minutes'))",(t,))
    c.commit(); c.close(); return t
def tok_del(t):
    c = sqlite3.connect(DB); c.execute("DELETE FROM sessions WHERE token=?",(t,)); c.commit(); c.close()

async def main():
    t = tok_add()
    try:
        async with websockets.connect(f"ws://127.0.0.1:8768/api/devices/{DEVICE}/shell?token={t}", max_size=None) as ws:
            buf = bytearray()
            async def drain(marker, timeout=30):
                dl = time.monotonic()+timeout
                while time.monotonic() < dl:
                    try: m = await asyncio.wait_for(ws.recv(), timeout=dl-time.monotonic())
                    except asyncio.TimeoutError: break
                    if isinstance(m, str): continue
                    buf.extend(m)
                    if marker in bytes(buf[-100:]): return True
                return False
            await drain(b"# ")
            await ws.send(b"\x00busybox stty -echo 2>/dev/null\n"); await drain(b"# ", 5)
            buf.clear()
            await ws.send(b"\x00echo ST\"\"ART; busybox base64 " + SRC.encode() + b"; echo E\"\"ND\n")
            ok = await drain(b"\nE" + b"ND", 120)
            text = bytes(buf).decode("utf-8", "replace")
            m = re.search(r"ST" "ART\r?\n(.*?)\r?\nE" "ND", text, re.S)
            if not m: print("FAILED to frame output; got", len(text), "bytes", file=sys.stderr); return
            b64 = re.sub(r"[^A-Za-z0-9+/=]", "", m.group(1))
            data = base64.b64decode(b64)
            sys.stdout.buffer.write(data)
            print(f"decoded {len(data)} bytes", file=sys.stderr)
    finally:
        tok_del(t)
asyncio.run(main())
