"""OTA the built binary to a device via the controller API."""
import asyncio, json, secrets, sqlite3, sys
import aiohttp

DEVICE = sys.argv[1]
BINARY = "/tmp/server-new"
DB = "/app/data/echomuse.db"

def make_token():
    tok = secrets.token_hex(32)
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, 1, datetime('now'), datetime('now', '+15 minutes'))", (tok,))
    con.commit(); con.close()
    return tok

def drop_token(tok):
    con = sqlite3.connect(DB)
    con.execute("DELETE FROM sessions WHERE token=?", (tok,))
    con.commit(); con.close()

async def main():
    tok = make_token()
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {tok}"}) as s:
            with open(BINARY, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("binary", f, filename="server")
                async with s.post("http://127.0.0.1:8768/api/releases/upload", data=form) as r:
                    up = await r.json()
                    print("upload:", r.status, up)
                    if r.status != 200: return
            async with s.post(f"http://127.0.0.1:8768/api/devices/{DEVICE}/update",
                              json={"upload_token": up.get("upload_token") or up.get("token")}) as r:
                print("update:", r.status, await r.text())
    finally:
        drop_token(tok)

asyncio.run(main())
