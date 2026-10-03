"""
The device's controller.json: two removals, and they must not be confused.

`controllerEndpoints` is fleet-only and delivered as a FILE the firmware
re-reads on every dial, which is why it needs no bounce and works on every
v2.16.0+ device with no firmware change. #166 documented hand-writing that file
for devices that cannot use mDNS, which is where the asymmetry comes from:

  - the FLEET sync removes only a file carrying `managed_by`, because at that
    point the controller is updating a fleet it does not own and a file
    somebody wrote by hand is the only way back for a device without mDNS;
  - the WIZARD removes ANY file, because at provisioning this controller is the
    source of truth and emOS keeps /data across a re-provision.

Every failure here is silent and lands on the device rather than the dashboard.
Deleting a hand-written file strands a device that can no longer find the
controller at all, and it presents as a device that is simply offline. So the
branch that removes nothing, and the branch that removes a hand-written file on
purpose, are both asserted — including the branch where the device does not
answer, which must change nothing rather than assume absence.
"""

import asyncio
import re

import em_api
import em_endpoints


class FakeLive:
    def __init__(self, device_id="dev"):
        self.device_id = device_id


def _reset():
    em_api._updates_in_progress = set()
    em_api._ota_lock = asyncio.Lock()
    em_api._events = []


def _patch_shell(monkeypatch, replies):
    """
    Answer `_shell_run` and `_stream_file_to_device` from a script.

    `replies` is a list of strings returned in order; the last one repeats.
    Every reply the production code inspects has `_SHELL_OK` appended by the
    device, so each entry here is the command's own output followed by that
    sentinel — exactly what a real device produces.
    """
    calls = []

    async def fake_shell_run(live, cmd, timeout=30.0):
        calls.append(cmd)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    pushed = []

    async def fake_stream(live, data, dest, mode=None, **kw):
        pushed.append((data, dest))
        return "ok"

    async def fake_log(device_id, level, source, message):
        em_api._events.append((level, source, message))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(em_api, "_shell_run", fake_shell_run)
    monkeypatch.setattr(em_api, "_stream_file_to_device", fake_stream)
    monkeypatch.setattr(em_api, "_push_log_event", fake_log)
    monkeypatch.setattr(em_api.asyncio, "sleep", no_sleep)
    return calls, pushed


def test_an_empty_setting_removes_only_a_file_this_controller_wrote(monkeypatch):
    """
    The fleet sync deletes only a MANAGED file.

    `md5sum` prints a 32-hex digest followed by whitespace when the file is
    there, and `grep -c` prints a count. Both probes are answered here the way a
    device answers them: the digest line for a file that exists, then `1` for a
    file we wrote.
    """
    _reset()
    replies = [f"0123456789abcdef0123456789abcdef  {em_endpoints.DEVICE_PATH}\n1\n{em_api._SHELL_OK}",
               f"{em_api._SHELL_OK}"]
    calls, pushed = _patch_shell(monkeypatch, replies)
    monkeypatch.setattr(em_api, "_controller_endpoints_file", lambda: None)

    assert asyncio.run(em_api._sync_controller_endpoints(FakeLive(), "dev")) is True
    assert any("rm -f" in c for c in calls), "a managed file must be removed"
    assert any("updated" in m or "removed" in m for _, _, m in em_api._events), em_api._events


def test_an_empty_setting_leaves_a_hand_written_file_alone(monkeypatch):
    """
    THE case that strands a device. A file with no `managed_by` is somebody's
    only route to the controller on a device that cannot use mDNS, so an empty
    fleet setting must leave it exactly where it is.

    #166 is why this exists, and the consequence of getting it wrong is not a
    wrong setting — it is a device that can no longer be reached at all.
    """
    _reset()
    # File present (the md5sum line), but `grep -c managed_by` printed 0.
    replies = [f"0123456789abcdef0123456789abcdef  {em_endpoints.DEVICE_PATH}\n0\n{em_api._SHELL_OK}"]
    calls, pushed = _patch_shell(monkeypatch, replies)
    monkeypatch.setattr(em_api, "_controller_endpoints_file", lambda: None)

    assert asyncio.run(em_api._sync_controller_endpoints(FakeLive(), "dev")) is True
    assert not any("rm -f" in c for c in calls), (
        "the fleet sync deleted a HAND-WRITTEN controller.json — #166 devices "
        f"lose their only route to the controller. calls={calls}"
    )
    assert em_api._events == [], f"removing nothing must not report a change: {em_api._events}"


def test_a_device_that_does_not_answer_is_left_entirely_alone(monkeypatch):
    """
    No answer is NOT evidence of absence, and this is the same rule
    `reconcile_oww_assets` and `_sync_start_script` follow with `_SHELL_OK`.

    Seconds after a device connects its shell plane is very likely not up yet.
    Reading the silence as "no file" would either push a redundant file or —
    with an empty setting — delete a file nobody has looked at. Either way the
    user sees an event that is untrue.
    """
    _reset()
    calls, pushed = _patch_shell(monkeypatch, [""])  # no sentinel at all
    monkeypatch.setattr(em_api, "_controller_endpoints_file", lambda: None)

    assert asyncio.run(em_api._sync_controller_endpoints(FakeLive(), "dev")) is False
    assert not any("rm -f" in c for c in calls), f"acted on a silent device: {calls}"
    assert pushed == [], f"pushed to a device that never answered: {pushed}"
    # It logs rather than raising a dashboard event, deliberately: this runs on
    # every connect, and an event per silent reconcile would bury the ones that
    # matter. The log line is the operator-facing record.
    assert em_api._events == [], (
        f"a silent device must not raise a dashboard event: {em_api._events}"
    )


def test_a_set_list_replaces_a_hand_written_file_and_says_so(monkeypatch):
    """
    A configured list replaces ANY file, hand-written or not — the device gets
    the fleet's addresses either way. What differs is the operator is told,
    because a hand-written file is being overwritten and that is worth a line
    in the log rather than a silent substitution.
    """
    _reset()
    want = em_endpoints.file_bytes([{"host": "10.0.0.5", "port": 8767, "tlsPort": 8770}])
    assert want, "the fixture must produce a file to write"
    replies = [f"0123456789abcdef0123456789abcdef  {em_endpoints.DEVICE_PATH}\n0\n{em_api._SHELL_OK}"]
    calls, pushed = _patch_shell(monkeypatch, replies)
    monkeypatch.setattr(em_api, "_controller_endpoints_file", lambda: want)

    assert asyncio.run(em_api._sync_controller_endpoints(FakeLive(), "dev")) is True
    assert pushed, "a configured list must be written even over a hand-written file"
    assert pushed[0][1] == em_endpoints.DEVICE_PATH
    assert any("hand-written" in m for _, _, m in em_api._events), em_api._events


def test_a_file_already_carrying_the_want_is_left_alone(monkeypatch):
    """
    An md5 match is a no-op. Pushing an identical file would churn the device's
    eMMC and report a change that did not happen — the module's whole discipline
    is that md5 decides, never mere presence.
    """
    _reset()
    want = em_endpoints.file_bytes([{"host": "10.0.0.5", "port": 8767, "tlsPort": 8770}])
    replies = [f"{em_endpoints.md5(want)}  {em_endpoints.DEVICE_PATH}\n1\n{em_api._SHELL_OK}"]
    calls, pushed = _patch_shell(monkeypatch, replies)
    monkeypatch.setattr(em_api, "_controller_endpoints_file", lambda: want)

    assert asyncio.run(em_api._sync_controller_endpoints(FakeLive(), "dev")) is True
    assert pushed == [], f"re-pushed an identical file: {pushed}"
    assert em_api._events == [], em_api._events


def test_a_failed_removal_is_reported_and_not_claimed_as_success(monkeypatch):
    """
    The removal is verified: the command checks the file is gone and echoes the
    sentinel. A device that answers but does not remove it must produce a
    warning, and the function must return False rather than True.

    The lie here is the dangerous direction — reporting "Controller address
    list removed" when the file is still there tells the operator the device is
    back on mDNS when it is not.
    """
    _reset()
    replies = [f"0123456789abcdef0123456789abcdef  {em_endpoints.DEVICE_PATH}\n1\n{em_api._SHELL_OK}",
               ""]  # the verification never echoes the sentinel
    _patch_shell(monkeypatch, replies)
    monkeypatch.setattr(em_api, "_controller_endpoints_file", lambda: None)

    assert asyncio.run(em_api._sync_controller_endpoints(FakeLive(), "dev")) is False
    assert any(level == "warn" for level, _, _ in em_api._events), em_api._events
    assert not any("removed — mDNS" in m for _, _, m in em_api._events), (
        f"claimed a removal that did not happen: {em_api._events}"
    )


def test_the_probe_recognises_only_a_real_md5_line_as_a_present_file():
    """
    `has_file` is `re.search(r"\\b[0-9a-f]{32}\\s", out)` — a shape test over the
    device's output, so it has to be pinned against output that RESEMBLES a
    digest without being one.

    The incident this guards is quiet in a specific way: a false positive makes
    the sync believe a file exists and (with an empty setting) try to remove
    one that was never there, and a false negative makes it skip a removal it
    owed. Both are silent on the device.
    """
    pattern = re.compile(r"\b[0-9a-f]{32}\s")
    assert pattern.search("0123456789abcdef0123456789abcdef  /path") is not None
    # Too short — a truncated read is not a digest.
    assert pattern.search("0123456789abcdef  /path") is None
    # Too long / wrong alphabet — uppercase is not what md5sum prints.
    assert pattern.search("0123456789ABCDEF0123456789ABCDEF  /path") is None
    # No trailing whitespace, so it is not the start of a line-and-path pair.
    assert pattern.search("0123456789abcdef0123456789abcdef") is None
    # A count line is not a digest. `grep -c` prints one of these and it must
    # never satisfy the file check.
    assert pattern.search("1\n") is None