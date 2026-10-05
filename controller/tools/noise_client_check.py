#!/usr/bin/env python3
"""
Check our encrypted ESPHome framing against Home Assistant's own client.

esphome/noise.py is tested against a published Noise vector, and the framing
around it against a test initiator — but both of those are OUR reading of how
ESPHome wraps Noise (the prologue, the hello frames, the reject strings).
This runs the real `aioesphomeapi` client against a real listener:

    right key   connects, and the device info round-trips
    wrong key   InvalidEncryptionKeyAPIError  (HA asks for the key again)
    no key      RequiresEncryptionAPIError    (HA asks for a key at all)

and with --plain, a client that still holds a key against a listener that no
longer has one (connections switched off again):

    EncryptionPlaintextAPIError   (HA offers to drop the key)

Two processes, because aioesphomeapi registers its own api.proto and ours is
vendored from a different release: importing both in one interpreter collides
in protobuf's descriptor pool.

    docker run --rm -v "$PWD":/c -w /c python:3.12 sh -c \\
      'pip install -q aioesphomeapi && python tools/noise_client_check.py'
"""
import asyncio
import base64
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 16999
NAME = "noise-check"
MAC = "02ec00000001"


def serve(key_hex: str) -> None:
    sys.path.insert(0, HERE)
    from esphome.satellite_server import SatelliteServerProtocol
    from esphome.vendor import api_pb2

    class Device(SatelliteServerProtocol):
        def __init__(self):
            super().__init__(server_name=NAME, log_name="check")
            if key_hex != "plain":
                self.set_encryption(bytes.fromhex(key_hex), NAME, MAC)

        def handle_message(self, msg):
            if isinstance(msg, api_pb2.DeviceInfoRequest):
                yield api_pb2.DeviceInfoResponse(
                    name=NAME, mac_address="02:EC:00:00:00:01",
                    # Long enough to need more than one TCP segment's worth
                    # of ciphertext in a single frame.
                    friendly_name="x" * 4000)

    async def main():
        loop = asyncio.get_running_loop()
        server = await loop.create_server(Device, "127.0.0.1", PORT)
        print("listening", flush=True)
        async with server:
            await server.serve_forever()

    asyncio.run(main())


async def client(key: bytes) -> int:
    import aioesphomeapi
    from aioesphomeapi import (InvalidEncryptionKeyAPIError,
                               RequiresEncryptionAPIError)

    async def attempt(psk):
        c = aioesphomeapi.APIClient("127.0.0.1", PORT, None, noise_psk=psk,
                                    expected_name=NAME)
        try:
            await c.connect(login=True)
            return await c.device_info()
        finally:
            await c.disconnect(force=True)

    failed = 0

    info = await attempt(base64.b64encode(key).decode())
    ok = info.name == NAME and info.friendly_name == "x" * 4000
    print(("PASS" if ok else "FAIL"), "right key: connected, device info round-tripped")
    failed += not ok

    for label, psk, want in (
        ("wrong key", base64.b64encode(bytes(32)).decode(), InvalidEncryptionKeyAPIError),
        ("no key", None, RequiresEncryptionAPIError),
    ):
        try:
            await attempt(psk)
            print("FAIL", label, "connected")
            failed += 1
        except want as e:
            print("PASS", f"{label}: {type(e).__name__}")
        except Exception as e:  # the wrong refusal is a failure too
            print("FAIL", f"{label}: {type(e).__name__}: {e}")
            failed += 1
    return failed


async def plain_client(key: bytes) -> int:
    """A client still holding a key meets a listener that no longer wants one."""
    import aioesphomeapi
    c = aioesphomeapi.APIClient("127.0.0.1", PORT, None,
                                noise_psk=base64.b64encode(key).decode())
    try:
        await c.connect(login=True)
        print("FAIL keyed client on a plaintext port: connected")
        return 1
    except Exception as e:
        ok = "EncryptionPlaintext" in type(e).__name__
        print("PASS" if ok else "FAIL",
              f"keyed client on a plaintext port: {type(e).__name__}")
        return 0 if ok else 1
    finally:
        await c.disconnect(force=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve(sys.argv[2])
        sys.exit(0)
    if len(sys.argv) == 2 and sys.argv[1] == "--plain":
        proc = subprocess.Popen([sys.executable, __file__, "--serve", "plain"],
                                stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout.readline().strip() == "listening"
            sys.exit(asyncio.run(plain_client(os.urandom(32))))
        finally:
            proc.terminate()
    key = os.urandom(32)
    proc = subprocess.Popen([sys.executable, __file__, "--serve", key.hex()],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "listening"
        import aioesphomeapi
        print("aioesphomeapi", getattr(aioesphomeapi, "__version__", "?"))
        sys.exit(1 if asyncio.run(client(key)) else 0)
    finally:
        proc.terminate()
