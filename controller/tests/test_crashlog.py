"""Classifying an emOS device's saved kernel log (em_crashlog)."""
import em_crashlog as cl

# The end of a real clean restart off EFF, 2026-09-17. MediaTek dumps a call
# trace on EVERY restart, so the trace must not read as a crash.
CLEAN = """\
[94998.740933] <0> (0)[1:init][name:leds_is31fl3236&]ISSI: resetting device before reboot!
[94999.351446] <0> (0)[1:init][name:reboot&]reboot: Restarting system
[94999.356218] <0> (0)[1:init][name:traps&]Call trace:
[94999.362867] <0> (0)[1:init][<ffffffc0000bba34>] do_kernel_restart+0x18/0x24
[94999.364587] <0> (0)[1:init][<ffffffc0000bbb40>] kernel_restart+0x64/0x74
[94999.365425] <0> (0)[1:init][<ffffffc0000bbe3c>] SyS_reboot+0xf0/0x200
"""

# The FireOS 6 kernel's form, with a restart command, off the spare.
CLEAN_CMD = "[ 1265.831325] <0> (0)[1:init][name:reboot&]reboot: Restarting system with command 'shell'\n"

# The shape of C95's crash (2026-09-04): a bus read timeout in the audio IRQ
# handler that faulted again in its own printk. No panic line.
CRASH = "".join(
    [f"[64460.{i:06d}] <1> (1)[0:swapper/1]filler line {i}\n" for i in range(50)]
    + [
        "[64463.012000] <1> (1)[118:mtk_stp_btm][STP-BTM] set fw issue infor fail(-15)\n",
        "[64463.700000] <1> (1)[472:tx_thread][wlan]wlanoidSetConnect:(OID INFO)ssid XXXXXX, bssid 74:ac:b9:d6:9b:22\n",
        "[64463.800000] <1> (1)[472:tx_thread][wlan] peer 10.10.1.81 unreachable\n",
        "[64468.800000] <0> (0)[0:swapper/0]Internal error: : 1d [#1] PREEMPT SMP\n",
        "[64468.800100] <0> (0)[0:swapper/0][<ffffffc0000824c0>] el1_da+0x18/0x78\n",
        "[64468.800200] <0> (0)[0:swapper/0][<ffffffc000082120>] do_mem_abort+0x40/0xa0\n",
        "[64468.800300] <0> (0)[0:swapper/0][<ffffffc0003a1234>] read_timeout_handler+0x10/0x30\n",
    ]
    + [f"[64468.9{i:05d}] <0> (0)[0:swapper/0] recursion {i}\n" for i in range(200)]
)


def test_a_clean_restart_is_not_reported():
    assert cl.is_clean(CLEAN)
    assert cl.summarise(CLEAN) is None
    assert cl.summarise(CLEAN_CMD) is None


def test_a_crash_is_reported_around_its_first_marker():
    msg = cl.summarise(CRASH)
    assert msg is not None
    assert "without a clean restart after 64468s" in msg
    assert "Internal error" in msg and "el1_da" in msg
    assert "around the first crash marker" in msg
    # 30 lines before the marker, so the firmware failure that preceded it
    # is in the excerpt too
    assert "set fw issue infor fail" in msg
    # and it does not run to the end of a thousand-line recursion
    assert "recursion 199" not in msg


def test_no_marker_falls_back_to_the_tail():
    # a watchdog reset can end the log with nothing to anchor on
    text = "".join(f"[  {i}.000000] <1> ordinary line {i}\n" for i in range(100))
    msg = cl.summarise(text)
    assert "no crash marker found; last 40 lines" in msg
    assert "ordinary line 99" in msg and "ordinary line 59" not in msg


def test_the_excerpt_carries_no_network_identifiers():
    msg = cl.summarise(CRASH)
    assert "ssid" not in msg.lower()
    assert "74:ac:b9" not in msg
    assert "10.10.1.81" not in msg and "<ip>" in msg


def test_the_excerpt_is_bounded():
    text = "[    1.000000] Oops " + "x" * 50000 + "\n"
    msg = cl.summarise(text)
    assert len(msg) < cl.MAX_CHARS + 400
    assert msg.endswith("…(truncated)")


def test_an_empty_log_is_reported_not_skipped():
    # an empty copy is not evidence of a clean restart
    assert cl.summarise("") is not None
