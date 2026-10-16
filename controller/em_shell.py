"""
Root shell sessions on a device, over the /shell plane: one programmatic
session per device at a time, commands with a deadline, and file transfers
verified by md5 before they replace anything.

The device dials /shell when asked (`shell_open`), and em_controller's
handle_shell hands the socket over through em_state.shell_pending. Everything
the controller does to a device beyond the control messages goes through
here: OTA, payload syncs, wake word assets, credentials, the emOS update.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath as _posixpath
import time
from shlex import quote as _sh_quote

import em_state

log = logging.getLogger("echomuse.api")

# How long to wait before asking again when the shell plane did not answer:
# seconds after a register it is often not up yet.
CONNECT_RETRY_S = 10.0

sockets: dict = {}   # device_id → live ws for programmatic sessions
locks:   dict = {}   # device_id → asyncio.Lock (one session at a time)
# device_id → the asyncio Task that actually holds its lock.
#
# `Lock.locked()` answers "is anyone holding this", never "am I", and the
# cleanup paths used it as though it meant the second. So a caller that gave
# up WAITING for the lock ran the same cleanup as one that had it, and
# released the lock out from under the transfer still using it — two shell
# sessions on one device, which is the exact thing the lock exists to stop.
# Seen end to end on EFF 2026-09-04: a debloat push hung for 108s, the wake
# word reconcile behind it timed out and released the debloat's lock, and the
# slot detect that followed then failed with `Lock is not acquired` and
# reported an empty result — surfacing to the operator as "could not
# determine active slot", three steps from anything to do with locking.
owners:  dict = {}   # device_id → task holding its lock


async def get_ws(live) -> object:
    """
    Request a programmatic shell connection from the device.

    Acquires a per-device lock so sessions are strictly sequential.
    handle_shell resolves the future with the ws, then waits for ws.close()
    before returning — so the connection stays alive while we use it.
    """
    device_id = live.device_id
    loop      = asyncio.get_event_loop()

    if device_id not in locks:
        locks[device_id] = asyncio.Lock()

    try:
        await asyncio.wait_for(locks[device_id].acquire(), timeout=20.0)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Shell lock acquisition timed out for {device_id}")
    # Claimed immediately after a successful acquire and never before it:
    # everything that cleans up asks whether it is the owner, so a caller
    # that timed out above must not be able to answer yes.
    owners[device_id] = asyncio.current_task()

    future = loop.create_future()
    em_state.shell_pending[device_id] = future
    # Deliberately do NOT set em_state.shell_dashboard — signals programmatic mode.

    await live.send_control({"type": "shell_open"})
    try:
        ws = await asyncio.wait_for(future, timeout=15.0)
        sockets[device_id] = ws
        return ws
    except asyncio.TimeoutError:
        em_state.shell_pending.pop(device_id, None)
        sockets.pop(device_id, None)
        release_lock(device_id)
        raise


def release_lock(device_id: str) -> None:
    """
    Release the shell lock, but only if THIS task is the one holding it.

    `Lock.locked()` cannot answer that question — it says whether anyone
    holds the lock — so guarding a release with it lets a caller that never
    acquired release somebody else's. See the owners comment.
    """
    if owners.get(device_id) is not asyncio.current_task():
        return
    owners.pop(device_id, None)
    lock = locks.get(device_id)
    if lock and lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass


async def release_ws(device_id: str, live=None) -> None:
    """
    Close the programmatic shell session.

    Closing ws wakes handle_shell's ws.wait_closed(), which then returns
    and lets the device clean up its side too.

    **A task that does not hold the lock cleans up nothing.** Callers run
    this from a `finally`, which is reached whether or not the acquire
    succeeded — so a caller that timed out waiting used to close the
    websocket belonging to the transfer that was still using it, send that
    device `shell_close`, and release its lock. Every one of those is an
    action against another operation's session.
    """
    if owners.get(device_id) is not asyncio.current_task():
        return
    ws = sockets.pop(device_id, None)
    if ws:
        try:
            await ws.close()
        except Exception:
            pass
    em_state.shell_pending.pop(device_id, None)
    if live is not None:
        await live.send_control({"type": "shell_close"})
    release_lock(device_id)


async def run(live, cmd: str, timeout: float = 30.0) -> str:
    """
    Run a shell command on the device and return its stdout as a string.

    Appends a sentinel marker to detect when output is complete.
    """
    SENTINEL = "__CMD_DONE_9f3a__"
    device_id = live.device_id
    output: list[str] = []
    try:
        ws = await get_ws(live)
        await ws.send(f"{cmd} ; echo '{SENTINEL}'\n")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=5.0)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                if SENTINEL in text:
                    output.append(text[:text.index(SENTINEL)])
                    break
                output.append(text)
            except asyncio.TimeoutError:
                break
        return "".join(output).strip()
    except Exception as e:
        log.error(f"[api] shell_run failed ({cmd!r}): {e}")
        return ""
    finally:
        await release_ws(device_id, live)


class TransferResult:
    """
    The outcome of a device file transfer, carrying the STAGE it stopped at.

    Truthy on success, so every existing `if not await stream_file(
    ...)` call site keeps working unchanged.

    The stage exists because one message covered five different outcomes, and
    the two that matter most are at opposite ends: "the bytes arrived corrupt"
    and "we never opened a shell, so no byte was ever sent". #121 reported the
    second and read as the first — three Dots failing with `failed or did not
    verify`, fifteen seconds after starting, which is far too fast to have
    attempted a 10MB payload. Nothing short of the controller's own stdout
    could tell them apart, and a user cannot be asked for that mid-update.
    """

    __slots__ = ("ok", "stage", "detail")

    def __init__(self, ok: bool, stage: str = "ok", detail: str = ""):
        self.ok     = ok
        self.stage  = stage
        self.detail = detail

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        return self.detail or self.stage


# Stage detail text. Phrased for someone reading a device log who is deciding
# what to try next, so each one says whether any data left the controller —
# that is the difference between "retry, the link was bad" and "the payload or
# the device is wrong".
_TRANSFER_STAGES = {
    "shell":   "could not open a shell session on the device — no data was sent",
    "decoder": "no base64 decoder found on the device — no data was sent",
    "md5tool": "device has no md5 tool, refusing to send unverified",
    "send":    "timed out part-way through sending",
    "verify":  "sent, but timed out waiting for md5 verification",
    "corrupt": "arrived corrupt — md5 did not match what was sent",
    "error":   "transfer error",
}


# Lines of base64 to enqueue before handing the event loop back.
#
# websockets' send() yields only when the write buffer is full. A firmware
# heredoc is about 200,000 lines, and while the socket keeps accepting them
# that loop never returns to anything else. One Dot's upload stalled the
# loop 2.2s (the base64 encode, now off this loop) and 13.8s (this send) on
# 2026-10-16, and every other Echo's wake handling waited. 32 lines is about
# 2.4KB; at the rate that covered a firmware binary, a batch is a couple of
# milliseconds, inside one 80ms wake frame.
_SEND_YIELD_LINES = 32


def _b64_lines(data: bytes) -> list[str]:
    """base64 of ``data``, one encodebytes line per entry, newline included.

    The heredoc body. Runs off the event loop: encoding a firmware binary is
    the 2.2s stall measured with the send.
    """
    import base64
    return base64.encodebytes(data).decode("ascii").splitlines(keepends=True)


async def _send_b64_lines(ws, lines: list[str]) -> None:
    for i, line in enumerate(lines, 1):
        await ws.send(line)
        if i % _SEND_YIELD_LINES == 0:
            await asyncio.sleep(0)


def _transfer_failed(stage: str, extra: str = "") -> TransferResult:
    detail = _TRANSFER_STAGES.get(stage, stage)
    if extra:
        detail = f"{detail} ({extra})"
    return TransferResult(False, stage, detail)


async def stream_file(live, data: bytes, dest: str,
                                 mode: str = "755",
                                 require_verify: bool = False) -> TransferResult:
    """
    Transfer a file to `dest` on the device via shell heredoc (default mode 755).

    Detects available base64 decoder (busybox base64, python3, python) before
    transferring, since 'base64' is not always in PATH on Android/FireOS.
    Uses a heredoc so no intermediate .b64 file is needed.
    The heredoc delimiter contains '_' which is not in the base64 alphabet.

    **md5 decides success, not the shell's exit status.** Bytes land in
    `{dest}.part` and are renamed only once their md5 matches what we sent, so
    a bad transfer leaves whatever was at `dest` untouched. `TRANSFER_OK` alone
    only ever proved that the decode pipeline and chmod exited 0 — not that the
    bytes arrived intact (#76). The verification rides the SAME shell session as
    the transfer, so it costs a round trip on an open socket, not a new session.

    `require_verify` decides what happens when the device has no md5 tool at
    all. Callers default to False, which warns and accepts — the same behaviour
    they had before this existed, and the base64 detection below already treats
    a busybox-less device as a contemplated state. Firmware passes True.
    """
    device_id     = live.device_id
    DELIM         = "__END_B64_42__"
    DETECT_MARKER = "__DETECT_DONE__"

    try:
        try:
            ws = await get_ws(live)
        except Exception as e:
            log.error(f"[api] Could not open a shell to {device_id} for "
                      f"{dest}: {e}")
            return _transfer_failed("shell", str(e))

        # `dest` is NOT deleted here, and must not be. This used to open with
        # `rm -f {dest}` to clear a previous attempt, which for firmware means
        # deleting /data/local/bin/server_<inactive> — THE ROLLBACK SLOT —
        # before a single byte of the replacement had been sent. Every failed
        # OTA therefore left the device with a good active slot and an empty
        # partner, so a later crash-loop would flip the symlink onto nothing.
        # It also contradicted the message the user was shown, which promised
        # the slot was left untouched (#121). Nothing needs the delete: the
        # heredoc writes with `>`, which truncates, and a verified transfer
        # arrives by `mv` over whatever was there.

        # ── Detect available base64 decoder ──────────────────────────────────
        # Try busybox first (Magisk provides it), then python3/python.
        # We run a round-trip sanity test so we know the decode flag works.
        # The md5 tool is detected in the SAME round trip, not a second one.
        # busybox first for the same reason as the decoder (Magisk provides
        # it); bare md5sum as a fallback since some SKUs have it in PATH.
        await ws.send(
            "if echo dGVzdA== | busybox base64 -d >/dev/null 2>&1; then echo DECODER:busybox; "
            "elif python3 -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' </dev/null >/dev/null 2>&1; then echo DECODER:python3; "
            "elif python  -c 'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.read()))' </dev/null >/dev/null 2>&1; then echo DECODER:python; "
            "else echo DECODER:none; fi; "
            "if echo x | busybox md5sum >/dev/null 2>&1; then echo MD5:busybox; "
            "elif echo x | md5sum >/dev/null 2>&1; then echo MD5:plain; "
            f"else echo MD5:none; fi; "
            # Does the destination DIRECTORY exist? Rides the round trip that
            # was already happening, so it costs nothing.
            #
            # Without it a write into a directory that is not there fails, the
            # `echo TRANSFER_OK` after it never runs, and the transfer waits
            # out its full 120s timeout for a confirmation that can never
            # come — holding the device's shell lock throughout. Measured on
            # EFF 2026-09-04: the debloat payload targets Magisk's
            # /sbin/.core overlay, which a device not running Magisk has no
            # daemon to create, and every attempt cost two minutes and took
            # the next shell operation down with it.
            f"if [ -d {_sh_quote(_posixpath.dirname(dest) or '/')} ]; "
            f"then echo DESTDIR:ok; else echo DESTDIR:missing; fi; "
            f"echo {DETECT_MARKER}\n"
        )

        detect_buf = ""
        detect_dl  = time.monotonic() + 15
        while time.monotonic() < detect_dl:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=2)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                detect_buf += text
                if DETECT_MARKER in detect_buf:
                    break
            except asyncio.TimeoutError:
                continue

        # Checked before the decoder, because it is the more specific answer:
        # a device with a perfectly good base64 and nowhere to put the file is
        # not a device that failed to decode.
        if "DESTDIR:missing" in detect_buf:
            log.warning(f"[api] {device_id}: {dest} — the destination "
                        f"directory does not exist on this device; not sending")
            return _transfer_failed(
                "destination",
                f"{_posixpath.dirname(dest)} does not exist on the device")

        if "DECODER:busybox" in detect_buf:
            decode_cmd = "busybox base64 -d"
        elif "DECODER:python3" in detect_buf:
            decode_cmd = ("python3 -c "
                          "'import sys,base64; "
                          "sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))'")
        elif "DECODER:python" in detect_buf:
            decode_cmd = ("python -c "
                          "'import sys,base64; "
                          "sys.stdout.write(base64.b64decode(sys.stdin.read()))'")
        else:
            # Two very different things reach here. DETECT_MARKER present means
            # the device answered and genuinely has no decoder — a property of
            # that device, which retrying will not change. Absent means the
            # round trip produced nothing in 15s, i.e. the shell plane is not
            # carrying output, which is a link problem and IS worth retrying.
            # Reporting both as "no base64 decoder" sent #121 looking at the
            # wrong half.
            if DETECT_MARKER not in detect_buf:
                log.error(f"[api] Shell produced no output in 15s while probing "
                          f"{device_id} for a decoder — link problem, not a "
                          f"missing tool. Output so far: {detect_buf!r}")
                return _transfer_failed(
                    "shell", "device shell produced no output within 15s")
            log.error(f"[api] No base64 decoder found on device. "
                      f"Detection output: {detect_buf!r}")
            return _transfer_failed("decoder")

        log.info(f"[api] Decoder: {decode_cmd.split()[0]} {decode_cmd.split()[1]}")

        if "MD5:busybox" in detect_buf:
            md5_cmd = "busybox md5sum"
        elif "MD5:plain" in detect_buf:
            md5_cmd = "md5sum"
        else:
            md5_cmd = None
            if require_verify:
                log.error(f"[api] No md5 tool on device — refusing to transfer "
                          f"{dest} unverified. Detection output: {detect_buf!r}")
                return _transfer_failed("md5tool")
            log.warning(f"[api] No md5 tool on device — {dest} will be "
                        f"transferred WITHOUT verification")

        # ── Heredoc transfer ─────────────────────────────────────────────────
        lines = await asyncio.get_running_loop().run_in_executor(None, _b64_lines, data)
        log.info(f"[api] Transferring {len(data):,} bytes to {dest} "
                 f"({len(lines)} base64 lines via heredoc)")

        # Bytes land in .part; only a matching md5 promotes them to dest. With
        # no md5 tool there is nothing to promote against, so write straight to
        # dest and keep the old behaviour (require_verify already refused above
        # for the payloads where that is not acceptable).
        landing = f"{dest}.part" if md5_cmd else dest

        # Single shell command: decode heredoc → landing, set permissions,
        # confirm. chmod here rather than after the rename so the mode travels
        # with the file and dest is never briefly present with the wrong one.
        await ws.send(
            f"{decode_cmd} << '{DELIM}' > {landing} && "
            f"chmod {mode} {landing} && "
            f"echo TRANSFER_OK\n"
        )

        # Each line already ends with \n from encodebytes. The yield is inside
        # _send_b64_lines: send() does not return to the loop on its own.
        await _send_b64_lines(ws, lines)

        # Close heredoc; shell now executes the decode pipeline
        await ws.send(f"{DELIM}\n")
        log.info(f"[api] Heredoc sent — waiting for TRANSFER_OK")

        # Wait for confirmation (decode of ~13 MB on ARM takes a few seconds)
        deadline    = time.monotonic() + 120
        transferred = False
        while time.monotonic() < deadline:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=5)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                if "TRANSFER_OK" in text:
                    transferred = True
                    break
                if text.strip():
                    log.debug(f"[api] Shell output during transfer: {text!r}")
            except asyncio.TimeoutError:
                continue

        if not transferred:
            log.error(f"[api] Transfer to {dest} timed out waiting for TRANSFER_OK")
            return _transfer_failed("send")

        if md5_cmd is None:
            log.info(f"[api] Transfer to {dest} confirmed (unverified)")
            return TransferResult(True, "unverified")

        # ── Verify, then promote ─────────────────────────────────────────────
        # Same shell session, so this is a round trip on an open socket rather
        # than a new session. A mismatch removes the .part and leaves dest as
        # it was — for firmware that means the rollback slot keeps its previous
        # binary instead of being replaced by a broken one.
        # No `cut`: md5sum prints "<hash>  <path>", and the busybox-less branch
        # is exactly where `busybox cut` would not be there either. A case glob
        # needs no external tool at all.
        want = await asyncio.get_running_loop().run_in_executor(
            None, lambda: hashlib.md5(data).hexdigest())
        await ws.send(
            f'GOT=$({md5_cmd} {landing} 2>/dev/null); '
            f'case "$GOT" in {want}*) mv {landing} {dest} && echo VERIFY_OK ;; '
            f'*) rm -f {landing}; echo "VERIFY_BAD:$GOT" ;; esac\n'
        )

        verify_buf = ""
        verify_dl  = time.monotonic() + 120
        while time.monotonic() < verify_dl:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=5)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                verify_buf += text
                if "VERIFY_OK" in verify_buf:
                    log.info(f"[api] Transfer to {dest} confirmed "
                             f"({len(data):,} bytes, md5 {want})")
                    return TransferResult(True)
                if "VERIFY_BAD" in verify_buf:
                    got = verify_buf.split("VERIFY_BAD:")[-1].strip().split()
                    log.error(f"[api] Transfer to {dest} CORRUPT — md5 {want} "
                              f"expected, device reported {got[0] if got else '(none)'}. "
                              f"{dest} left untouched.")
                    return _transfer_failed("corrupt",
                                            f"device reported {got[0] if got else '(none)'}")
            except asyncio.TimeoutError:
                continue

        log.error(f"[api] Transfer to {dest} timed out waiting for md5 verification "
                  f"— {dest} left untouched")
        return _transfer_failed("verify")

    except Exception as e:
        log.error(f"[api] File transfer to {dest} failed: {e}")
        return _transfer_failed("error", str(e))
    finally:
        await release_ws(device_id, live)


# Appended to a probe command so an empty run result can be told apart
# from a command that ran and printed nothing. run swallows every
# exception and returns "", so without this "the file is missing" and "the
# device never answered" are the same string — and they want opposite actions.
OK_MARK = "__EM_SHELL_OK__"


async def send_command(live, cmd: str) -> None:
    """Send a command to the device shell and return immediately (fire-and-forget)."""
    try:
        ws = await get_ws(live)
        await ws.send(cmd + "\n")
        await asyncio.sleep(0.5)
    except Exception as e:
        log.warning(f"[api] Shell exec failed ({cmd!r}): {e}")
    finally:
        await release_ws(live.device_id, live)
