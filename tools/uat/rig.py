"""
Release UAT rig: an isolated controller, a USB Echo attached to it, a browser.

    rig.py up --image IMAGE      controller container + admin account
    rig.py attach SERIAL         point a USB Echo at it, approve it (repeat per Echo)
    rig.py detach SERIAL         put the Echo back exactly as it was
    rig.py down                  remove the controller container

The controller runs on a Docker BRIDGE network, not the host's: its mDNS then
stays inside the container, so no other Echo on the LAN can discover it and
wander off a soak or a household controller. The attached Echo reaches it by
address, through an endpoint file with mdns:false (so it cannot fall back to
the real controller either). Its own link credentials are copied aside first
and restored by detach — the rig never loses a device's pairing.

State (admin password, session token, which Echoes are attached) is kept in
UAT_STATE (default ~/.echomuse-uat), never in the repo.

An emOS Echo is reached over its USB serial console, a FireOS one over adb:
Echo(serial) picks by whether adb lists the serial.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "emos" / "tools"))
from emconsole import Console, list_consoles  # noqa: E402

STATE = Path(os.environ.get("UAT_STATE", Path.home() / ".echomuse-uat"))
CONTAINER = "em-uat"
BROWSER = "em-uat-browser"
PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright/python:v1.49.1-noble"
HOST_IP = os.environ.get("UAT_HOST_IP", "10.10.1.236")
PORT, TLS_PORT, API_PORT = 8767, 8770, 8778
API = f"http://127.0.0.1:{API_PORT}"
DEVICE_ETC = "/data/local/etc/echomuse"
BACKUP = f"{DEVICE_ETC}/uat-backup"


# ─── Controller API ──────────────────────────────────────────────────────────

def _state() -> dict:
    p = STATE / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(**kw) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    s = _state() | kw
    (STATE / "state.json").write_text(json.dumps(s, indent=1))
    os.chmod(STATE / "state.json", 0o600)


def api(method: str, path: str, body: dict | None = None, token: str | None = None):
    token = token or _state().get("token")
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def device(serial: str) -> dict | None:
    return next((d for d in api("GET", "/api/devices") if d["device_id"] == serial), None)


def wait_for(what: str, fn, timeout: float = 90, every: float = 2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(every)
    raise SystemExit(f"timed out waiting for {what}")


# ─── Echo over USB ───────────────────────────────────────────────────────────

def adb_serials() -> set[str]:
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("device")}


def Echo(serial: str):
    """The Echo with this serial, over adb (FireOS) or its serial console (emOS)."""
    return AdbEcho(serial) if serial in adb_serials() else EmosEcho(serial)


class AdbEcho:
    """
    A FireOS Echo on USB adb, as root.

    Each command is pushed as a script and run with `su -c sh <file>`, so
    nothing is interpolated into an adb shell line. FireOS 5's toolbox has no
    cp -p, killall or md5sum, so commands run with Magisk's busybox applets
    first on PATH, which also makes them the same commands the emOS console
    runs.
    """
    base = "fireos"
    BUSYBOX = "/data/adb/magisk/busybox"
    APPLETS = "/data/local/tmp/uat-bb"
    SCRIPT = "/data/local/tmp/uat-cmd.sh"

    def __init__(self, serial: str):
        self.serial = serial
        self._sh(f"mkdir -p {self.APPLETS} && {self.BUSYBOX} --install -s {self.APPLETS}")

    def _sh(self, body: str, timeout: int = 20) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(body + "\n")
        try:
            subprocess.run(["adb", "-s", self.serial, "push", f.name, self.SCRIPT],
                           capture_output=True, check=True, timeout=timeout)
        finally:
            os.unlink(f.name)
        r = subprocess.run(["adb", "-s", self.serial, "shell", f"su -c 'sh {self.SCRIPT}'"],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.replace("\r", "")

    def run(self, cmd: str, timeout: int = 20) -> str:
        return self._sh(f"export PATH={self.APPLETS}:$PATH\n{cmd}", timeout)

    def config_report(self) -> dict | None:
        out = self.run("cat /tmp/em-config.json 2>/dev/null")
        m = re.search(r"\{.*\}", out, re.S)
        return json.loads(m.group(0)) if m else None

    def restart_firmware(self) -> None:
        self.run("killall server")

    def close(self) -> None:
        pass


class EmosEcho:
    """An emOS Echo on its USB serial console, addressed by serial number."""
    base = "emos"

    def __init__(self, serial: str):
        ports = [p for p, s in list_consoles() if s == serial]
        if not ports:
            raise SystemExit(f"no USB console with serial {serial}")
        self.serial = serial
        # Clear a half-typed line a killed session may have left (a `>`
        # continuation prompt), or login waits on a prompt that never comes.
        with open(ports[0], "wb", buffering=0) as tty:
            tty.write(b"\x03\r\n")
        time.sleep(0.5)
        self.con = Console(ports[0])

    def run(self, cmd: str, timeout: int = 20) -> str:
        return self.con.run(cmd, timeout=timeout)

    def config_report(self) -> dict | None:
        """The device's own record of config received and applied."""
        out = self.run("cat /tmp/em-config.json 2>/dev/null")
        m = re.search(r"\{.*\}", out, re.S)
        return json.loads(m.group(0)) if m else None

    def restart_firmware(self) -> None:
        self.run("busybox killall server")

    def close(self) -> None:
        self.con.close()


def _hex(text: str) -> str:
    """Text for a device shell as printf escapes: nothing is interpolated."""
    return "".join(f"\\x{b:02x}" for b in text.encode())


# ─── Commands ────────────────────────────────────────────────────────────────

def up(image: str) -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    data = STATE / "controller-data"
    subprocess.run(["rm", "-rf", str(data)])
    data.mkdir(parents=True)
    subprocess.run([
        "docker", "run", "-d", "--name", CONTAINER,
        "-p", f"{PORT}:{PORT}", "-p", f"{TLS_PORT}:{TLS_PORT}", "-p", f"{API_PORT}:8768",
        "-e", f"SERVER_IP={HOST_IP}", "-e", f"SERVER_PORT={PORT}", "-e", "API_PORT=8768",
        "-e", "DB_PATH=/app/data/echomuse.db", "-e", "DEVICE_APPROVAL=strict",
        "-e", "VOICE_MODE=esphome", "-e", "MDNS_NAME=em-uat", "-e", "LOG_LEVEL=DEBUG",
        "-v", f"{data}:/app/data", image], check=True, capture_output=True)

    def setup_token():
        logs = subprocess.run(["docker", "logs", CONTAINER], capture_output=True, text=True)
        text = logs.stdout + logs.stderr
        block = text.split("first-run setup", 1)
        return "".join(re.findall(r"[0-9a-f]{32}", block[1]))[:64] if len(block) > 1 else None
    one_time = wait_for("the first-run setup token", setup_token, 60, 1)
    password = os.urandom(12).hex()
    res = api("POST", "/api/setup",
              {"token": one_time, "username": "uat", "password": password}, token="")
    _save_state(image=image, username="uat", password=password, token=res["token"])
    print(f"controller up: {image}, dashboard {API}, admin 'uat' (password in {STATE})")


def attach(serial: str) -> None:
    echo = Echo(serial)
    try:
        # Aside once: a second attach must not overwrite the originals with
        # the rig's own credentials. One short command per call: a long line
        # on the serial console confuses the framing emconsole reads.
        if "saved" not in echo.run(f"[ -e {BACKUP}/.saved ] && echo saved"):
            echo.run(f"mkdir -p {BACKUP}")
            for f in ("ca.pem", "token", "controller.json"):
                echo.run(f"[ -e {DEVICE_ETC}/{f} ] && cp -p {DEVICE_ETC}/{f} {BACKUP}/")
            echo.run(f"[ -e {DEVICE_ETC}/controller.json ] || touch {BACKUP}/.no-controller-json")
            echo.run(f"touch {BACKUP}/.saved")
        # Joins as a fresh device: its credentials belong to another
        # controller (they are in the backup, and detach restores them).
        echo.run(f"rm -f {DEVICE_ETC}/ca.pem {DEVICE_ETC}/token")
        endpoint = json.dumps({"endpoints": [{"host": HOST_IP, "port": PORT,
                                              "tls_port": TLS_PORT}], "mdns": False})
        echo.run(f"printf '{_hex(endpoint)}' > {DEVICE_ETC}/controller.json")
        echo.restart_firmware()
    finally:
        echo.close()
    wait_for(f"{serial} to reach the rig", lambda: device(serial), 120)
    # Labels must be unique (#650), and there is one per attached Echo.
    api("POST", f"/api/devices/{serial}/approve", {"label": f"UAT {serial[-4:]}"})
    d = wait_for(f"{serial} connected", lambda: (device(serial) or {}).get("connected")
                 and device(serial), 180)
    _save_state(serials=sorted(set(_state().get("serials") or []) | {serial}))
    print(f"{serial} attached: firmware {d.get('firmware_ver')}, link "
          f"{'wss' if d.get('linkTls') else 'plain'}")


def detach(serial: str) -> None:
    echo = Echo(serial)
    try:
        if "saved" not in echo.run(f"[ -e {BACKUP}/.saved ] && echo saved"):
            raise SystemExit(f"{serial} has no UAT backup to restore")
        for f in ("ca.pem", "token", "controller.json"):
            echo.run(f"[ -e {BACKUP}/{f} ] && cp -p {BACKUP}/{f} {DEVICE_ETC}/")
        echo.run(f"[ -e {BACKUP}/.no-controller-json ] && rm -f {DEVICE_ETC}/controller.json")
        echo.run(f"rm -rf {BACKUP}")
        echo.restart_firmware()
        if isinstance(echo, AdbEcho):
            echo._sh(f"rm -rf {AdbEcho.APPLETS} {AdbEcho.SCRIPT}")
    finally:
        echo.close()
    _save_state(serials=sorted(set(_state().get("serials") or []) - {serial}))
    print(f"{serial} restored to its own controller and credentials")


def down() -> None:
    for c in (CONTAINER, BROWSER):
        subprocess.run(["docker", "rm", "-f", c], capture_output=True)
    print("rig down")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up"); u.add_argument("--image", required=True)
    a = sub.add_parser("attach"); a.add_argument("serial")
    d = sub.add_parser("detach"); d.add_argument("serial")
    sub.add_parser("down")
    args = ap.parse_args()
    {"up": lambda: up(args.image), "attach": lambda: attach(args.serial),
     "detach": lambda: detach(args.serial), "down": down}[args.cmd]()
