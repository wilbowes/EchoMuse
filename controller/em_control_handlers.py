"""
One handler per device-to-controller control message (#910), looked up by
type in em_controller.CONTROL_HANDLERS: mute, volume, stats, playback stats,
wake and listen reports, Sendspin, Wi-Fi, BLE adverts, logs, pong. The
connection itself (registration, auth, teardown) stays in handle_control.

Moved out of em_controller (2026-11) with no change to the bodies.
"""

import asyncio
import contextlib
import logging
import time

import em_ble_proxy
import em_db as db
import em_dbwriter
import em_device
import em_device_view
import em_esphome as esphome
import em_events
import em_health
import em_listen
import em_shadow
import em_volume
import em_wake_claim
import em_wakeword

log = logging.getLogger("echomuse")


async def on_ambient_light(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    # A step change in room light, sent by the device the
    # moment it happens rather than waiting up to 30s for the
    # stats tick — the timing IS the signal ("someone turned
    # a light on"). The steady-state value still rides stats.
    _lux = msg.get("lux")
    if isinstance(_lux, int):
        # None until the first stats report; that report carries the
        # reading too, so there is nothing to hold until then.
        if device.stats is not None:
            device.stats["ambientLux"] = _lux
        esphome.update_ambient_lux(device_id, _lux)
        log.info(f"[{device_id}] Ambient light → {_lux} lux")


async def on_mute_state(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    device.muted = msg.get("muted", False)
    # The mic mute button never moves the wake word
    # (#286), but the device restarts its wake stream on
    # unmute, so take it back down if the wake word is off.
    was_muted, wake_on = esphome.get_mic_muted_and_wake_word(device_id)
    esphome.update_mic_muted(device_id, device.muted)
    if em_wakeword.on_mic_mute(was_muted=was_muted,
                               now_muted=device.muted,
                               enabled=wake_on):
        log.info(f"[{device_id}] Unmuted with the wake word off "
                 f"— stopping the stream the device restarted")
        await device.mic_stop()
    if device.muted and device.voice_lock.locked():
        # Mute during an active turn terminates it — same
        # cancel as the dot button, plus speaker_flush so
        # any in-flight TTS goes silent immediately (the
        # device shows the red ring the moment the button
        # is pressed; audio carrying on would contradict
        # it). The device guards its LED ring while muted,
        # so the cancelled turn's LED cleanup can't clear
        # the red ring.
        log.info(
            f"[{device_id}] Muted during active turn — "
            f"cancelling"
        )
        device.cancel_event.set()
        esphome.cancel_voice_turn(device_id, reason="muted")
        await device.send_control({"type": "speaker_flush"})
    await em_events.push({
        "type":      "device_update",
        "device_id": device_id,
        "state":     {"muted": device.muted,
                      "wake_word_enabled": device.wake_word_enabled},
    })


async def on_volume_state(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    loop = asyncio.get_event_loop()
    # Device reports its current volume level (raw tinymix index).
    # Convert to HA float, update in-memory state, persist to
    # config so the value survives controller and device restarts.
    raw_level = int(msg.get("level", 85))
    _keep, _send = esphome.output_mute_report(device_id, raw_level)
    if _send is not None:
        # Volume-up while muted: restore above the old
        # level; the device reports it back, and that
        # report is the one kept.
        await device.send_control({"type": "volume_set", "level": _send})
    if _keep:
        device.volume = em_volume.device_level_to_ha(raw_level)
        log.debug(
            f"[{device_id}] volume_state: level={raw_level} "
            f"→ {device.volume:.3f}"
        )
        # Persist — read-modify-write to avoid stomping other fields
        stored_config = await loop.run_in_executor(
            None, db.get_device_config, device_id
        )
        stored_config["startupVolume"] = raw_level
        await loop.run_in_executor(
            None, db.set_device_config, device_id, stored_config
        )
    # Otherwise it is our own output mute echoing back as
    # 0: not a volume anyone chose, so neither the startup
    # volume nor HA's slider takes it, and unmute restores
    # the level from before (#641). HA is told either way,
    # so the entity shows muted over the old level.
    esphome.update_device_volume(device_id, device.volume)


async def on_stats(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    loop = asyncio.get_event_loop()
    if "sendspin" in msg:
        device.sendspin = msg.get("sendspin")
    # eMMC wear, re-read on the device every few hours;
    # one row per day (schema v28), written on change.
    note_wear(device, msg.get("emmc"))
    device.stats = {
        "cpuPct":        msg.get("cpuPct"),
        "memUsedMb":     msg.get("memUsedMb"),
        "memTotalMb":    msg.get("memTotalMb"),
        "storageUsedMb": msg.get("storageUsedMb"),
        "storageTotalMb":msg.get("storageTotalMb"),
        "wifiRssi":      msg.get("wifiRssi"),
        "wifiSsid":      msg.get("wifiSsid"),
        # v7 link telemetry (firmware >= v2.9.6). This dict
        # is an explicit allowlist, so any new device stat
        # must be added HERE as well as in DeviceStats and
        # record_device_stats — all three, or the field is
        # silently dropped in the relay (2026-07-20).
        "linkSpeedMbps": msg.get("linkSpeedMbps"),
        "wifiFreqMhz":   msg.get("wifiFreqMhz"),
        "wifiBssid":     msg.get("wifiBssid"),
        "txBytes":       msg.get("txBytes"),
        "rxBytes":       msg.get("rxBytes"),
        "txErrors":      msg.get("txErrors"),
        "txDropped":     msg.get("txDropped"),
        "rxCrcErrors":   msg.get("rxCrcErrors"),
        # Uplink loss: the device's own TCP retransmits since
        # its last report (tcpUpSegs only where the kernel
        # counts segments; FireOS 5's does not). Downlink is
        # measured here, in Device.drain_tcp.
        "tcpUpRetrans":  msg.get("tcpUpRetrans"),
        "tcpUpSegs":     msg.get("tcpUpSegs"),
        "ble":           msg.get("ble"),
        # Thermals + CPU topology. coresOnline is not optional
        # context: cpuPct is a share of ONLINE capacity, so the
        # same work halves its percentage when hotplug adds a
        # core. thermalCoreLimit < coresTotal means the thermal
        # governor is capping capacity.
        "cpuTempC":         msg.get("cpuTempC"),
        "maxTempC":         msg.get("maxTempC"),
        "coresOnline":      msg.get("coresOnline"),
        "coresTotal":       msg.get("coresTotal"),
        "thermalCoreLimit": msg.get("thermalCoreLimit"),
        # Ambient light (TSL2540). None on a device without
        # the sensor — 0 lux is a real reading (covered), so
        # the two must not collapse into each other.
        "ambientLux":       msg.get("ambientLux"),
        # v13 on-device shadow window summary, absent when the
        # device is not scoring (see the allowlist note above:
        # DeviceStats, here, and the consumer below).
        "owwShadow":     msg.get("owwShadow"),
        # Which far-end reference the AEC is on: "hw",
        # "sw", "off", or absent from firmware that
        # cannot say. Deliberately NOT added to
        # record_device_stats, unlike the rest of this
        # allowlist: it is a state, not a metric, and
        # the hourly rollup averages numbers.
        "aecRef":        msg.get("aecRef"),
        "ssh":           msg.get("ssh"),  # {listening, hostKey}; a state
    }
    # Shadow summary → hourly rollup. Present only while the
    # device is scoring, so its presence is also the "was it
    # looking" flag that stops a missing per-turn score from
    # reading as a miss.
    _sh = msg.get("owwShadow") or {}
    device.shadow.active = bool(_sh)
    if _sh and _sh.get("threshold"):
        try:
            device.shadow.threshold = float(_sh["threshold"])
        except (TypeError, ValueError):
            pass
    if _sh:
        if _sh.get("drops") or _sh.get("errors"):
            log.warning(
                f"[{device_id}] on-device wake word fell behind: "
                f"{_sh.get('drops')} frames dropped, "
                f"{_sh.get('errors')} errors ({_sh.get('lastErr') or '-'}) "
                f"— comparison is running on a subset of the audio"
            )
    if msg.get("ble"):
        em_ble_proxy.update_stats(device_id, msg["ble"])
    # Ambient light straight through to HA's sensor entity.
    # Rides the existing ~30s stats tick — light does not
    # change fast enough to justify a channel of its own, and
    # nothing per-frame is the standing rule here.
    if "ambientLux" in msg:
        esphome.update_ambient_lux(device_id, msg.get("ambientLux"))
    # Temperatures the same way, on the same tick.
    if "cpuTempC" in msg or "maxTempC" in msg:
        esphome.update_temperatures(device_id, msg.get("cpuTempC"), msg.get("maxTempC"))
    # Fold into the persistent hourly rollup (CPU/RAM/storage/
    # RSSI trends) — one cheap upsert per ~30s report. The
    # last_seen refresh rides the same executor hop: a stats
    # report IS proof of life, and without it last_seen only
    # ever recorded the last *connect*.
    # RTT is controller-measured, not relayed from the
    # device message, so it is merged in here rather than
    # coming through the allowlist above. drain_rtt() takes
    # and resets the window accumulated since the last
    # report, so no sample is counted twice.
    _tcp = device.drain_tcp()
    device.tcp_minutes.add(time.time(), _tcp.get("tcpDownSegs"),
                           _tcp.get("tcpDownRetrans"))
    _metrics = {**device.stats, **device.drain_rtt(), **_tcp}
    def _persist_stats(_id=device_id, _s=_metrics, _shadow=_sh):
        db.record_device_stats(_id, _s)
        db.touch_device_seen(_id)
        # Shadow counters ride the SAME executor hop rather
        # than adding one: the whole point of summarising on
        # the device is that on-device scoring costs the DB
        # one upsert per 30s, not one per frame.
        if _shadow:
            db.bump_wake_counters(
                _id,
                dev_frames=int(_shadow.get("frames") or 0),
                dev_drops=int(_shadow.get("drops") or 0),
                dev_crossings=int(_shadow.get("crossings") or 0),
                dev_max_score=float(_shadow.get("maxScore") or 0.0),
                # v16: what a drop actually was — slowest
                # inference vs longest frame gap.
                dev_max_infer_ms=int(_shadow.get("maxInferMs") or 0),
                dev_max_gap_ms=int(_shadow.get("maxGapMs") or 0),
            )
    await loop.run_in_executor(None, _persist_stats)
    await em_events.push({
        "type":      "device_update",
        "device_id": device_id,
        "state":     {
            "stats": device.stats,
            # Controller-side proxy view rides along so the
            # dashboard's Bluetooth panel stays live without
            # a full device refresh.
            "bleProxy": em_ble_proxy.get_status(device_id),
            "sendspin": device.sendspin,
        },
    })


async def on_sendspin_status(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    # The player's state as it changes (pairing, a stream
    # starting); the stats tick carries it too.
    device.sendspin = msg.get("status")
    await em_events.push({
        "type": "device_update", "device_id": device_id,
        "state": {"sendspin": device.sendspin},
    })


async def on_sendspin_token(device: em_device.Device, msg: dict) -> None:
    # The answer to sendspin_token_request. A secret: it
    # goes to the one waiting request and nowhere else,
    # never a log line or an event.
    waiter = device.sendspin_token_waiter
    if waiter is not None and not waiter.done():
        waiter.set_result(msg)


async def on_wifi_result(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    # Outcome of a wifi_change. The device re-sends this
    # until it sees a wifi_commit ack (a single send can
    # vanish into a half-open TCP connection killed by the
    # network switch), so: ALWAYS ack — on success the ack
    # also finalises the change (deletes rollback backup +
    # pending marker; a failed change already removed both,
    # so the ack is a no-op there) — and log/record only
    # the first arrival.
    ok    = bool(msg.get("ok"))
    ssid  = msg.get("ssid", "")
    error = msg.get("error") or ""
    st, duplicate = em_device_view.wifi_record_result(device_id, ok, ssid, error)
    await device.send_control({"type": "wifi_commit"})
    if not duplicate:
        if ok:
            log.info(f"[{device_id}] WiFi changed to \"{ssid}\" — committed")
            em_dbwriter.submit(db.log_device, device_id, "info", "device",
                          f'WiFi changed to "{ssid}"')
        else:
            log.warning(f"[{device_id}] WiFi change to \"{ssid}\" "
                        f"failed: {error}")
            em_dbwriter.submit(db.log_device, device_id, "warning", "device",
                          f'WiFi change to "{ssid}" failed: {error}')
        await em_events.push({
            "type":      "device_update",
            "device_id": device_id,
            "state":     {"wifi": st},
        })


async def on_playback_stats(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    loop = asyncio.get_event_loop()
    # One report per completed speaker stream (firmware
    # >= v2.9): periods played + mid-stream underruns.
    # Attach to the turn persisted just before playback;
    # consume last_turn_id so a later announcement's report
    # can't overwrite a turn's stats. Reports with no
    # pending turn (HA announcements, TTS after a controller
    # restart) roll into the hourly counters instead.
    # periods/underruns are read from the top level, which
    # every firmware sends; "stats" carries the v2.9.6+
    # delivery-margin fields and is absent on older devices.
    periods   = int(msg.get("periods", 0))
    underruns = int(msg.get("underruns", 0))
    _bw = msg.get("barge")
    if isinstance(_bw, dict) and device.private_listening:
        # The private path's "Barge watcher done" line:
        # the Echo scores its own barge-in, so only it can
        # say how close one came. frames 0 = the barge bar
        # never applied (barge-in off).
        log.info(
            f"[{device_id}] Barge window (device): "
            f"{_bw.get('frames')} frames at bar "
            f"{float(_bw.get('bar') or 0):.2f}, peak "
            f"{float(_bw.get('peak') or 0):.3f}"
            + ("" if device.barge_in_enabled else " — barge-in off")
        )
    pstats    = msg.get("stats") or {}
    # Release _run_post_turn_playback: this report IS the
    # end of audio, and the ring clears on it rather than
    # on a wall-clock guess.
    device.signal_playback_done()
    # Delivery window: first speaker frame sent -> this
    # report. The metric the 07-20 investigation lacked —
    # "Streaming took Xs" times the socket write and reads
    # ~0s however slowly the device is really being fed.
    delivery_ms = -1
    if device.playback_send_t0 is not None:
        delivery_ms = int(
            (loop.time() - device.playback_send_t0) * 1000
        )
        device.playback_send_t0 = None
    turn_id   = device.last_turn_id
    device.last_turn_id = None
    if turn_id is not None:
        await loop.run_in_executor(
            None, db.set_turn_playback,
            turn_id, periods, underruns, pstats,
        )
        if delivery_ms >= 0:
            await loop.run_in_executor(
                None, db.set_turn_delivery, turn_id,
                device.playback_send_ms,
                delivery_ms, device.playback_eq_ms,
            )
        for rec in reversed(device.turn_history):
            if rec.get("turn_id") == turn_id:
                rec["playback_periods"] = periods
                rec["underruns"]        = underruns
                break
    else:
        # The turn row may not exist yet (device buffers
        # usually drain before the controller's drain sleep
        # ends) — stash for em_turn_record.persist_turn to fold in. A
        # displaced earlier stash was an announcement's:
        # keep its underruns in the hourly counters.
        prev = device.pending_playback_stats
        # Indices 0-2 stay (ts, periods, underruns) so the
        # existing consumer keeps working; 3-4 carry the v7
        # delivery detail.
        device.pending_playback_stats = (
            asyncio.get_event_loop().time(), periods, underruns,
            pstats, delivery_ms,
        )
        if prev and prev[2]:
            await loop.run_in_executor(
                None,
                lambda: db.bump_wake_counters(
                    device_id, underruns=prev[2]
                ),
            )
    if underruns:
        log.warning(
            f"[{device_id}] Playback underruns: {underruns} "
            f"in {periods} periods"
            f"{f' (turn {turn_id})' if turn_id else ''}"
        )


async def on_oww_shadow_cross(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    # On-device scoring reached the wake threshold. Recorded
    # for comparison ONLY — nothing here starts a turn, and
    # that is the entire point of shadow mode.
    device.shadow.record_cross(msg.get("score"), msg.get("ageMs"))
    _consider_on_device_wake_sample(
        device, msg, device.shadow.threshold
    )
    log.info(
        f"[{device_id}] on-device wake crossing: "
        f"score={msg.get('score')} age={msg.get('ageMs')}ms "
        f"(shadow — not triggering)"
    )


async def on_oww_wake(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    # On-device scoring crossed the bar AND owwOnDevice is
    # "on", so the device is asking for a turn rather than
    # reporting a measurement. Parked here; the wake listener
    # picks it up on its next frame (~80ms) because that is
    # where the turn setup lives — capture routing, beam lock
    # and arbitration all have to happen together, and doing
    # them from the control plane would be a second copy of
    # the most delicate sequence in the controller.
    #
    # Also recorded as a crossing, so a device-triggered turn
    # carries the same dev_* comparison fields as a
    # controller-triggered one and the Activity tab does not
    # have to special-case which side fired.
    device.shadow.record_cross(msg.get("score"), msg.get("ageMs"))
    if device.listen_view.streams:
        # Only when this Echo's mic is actually reaching us
        # continuously (#696 review) — a private session's
        # own pre-roll is never fed (see the data-plane 0x07
        # branch), so capturing it here would grab whatever
        # stale audio is left from a PRIOR session instead.
        _consider_on_device_wake_sample(
            device, msg, msg.get("threshold")
        )
    if msg.get("session"):
        # Private listening: the wake opened a session.
        ev = em_listen.parse_wake(msg, asyncio.get_event_loop().time())
        if ev is None:
            log.warning(f"[{device_id}] malformed oww_wake dropped: {msg!r}")
        else:
            try:
                device.listen_wakes.put_nowait(ev)
                _heard, _how = em_wake_claim.wake_dating(device, ev)
                log.info(
                    f"[{device_id}] on-device wake: score="
                    f"{ev['score']:.3f} age={ev['age_ms']}ms "
                    f"session={ev['session']}"
                    f"{' (over playback)' if ev['barge'] else ''}"
                    f", heard {(ev['arrived'] - _heard) * 1000:.0f}ms "
                    f"before arrival ({_how})"
                )
            except asyncio.QueueFull:
                await device.listen_close(ev["session"], "busy")
                log.warning(f"[{device_id}] wake queue full — "
                            f"session {ev['session']} closed")
    elif device.oww_on_device != em_shadow.MODE_ON:
        # Firmware triggering while the controller thinks it
        # should not: a config push in flight, or a rollback
        # to a mode this device no longer has. Logged rather
        # than obeyed — the controller's view of the mode is
        # the one the dashboard shows.
        log.warning(
            f"[{device_id}] oww_wake ignored — mode is "
            f"{device.oww_on_device!r}, not 'on'"
        )
    elif device.pending_wake.offer(
        msg.get("score"), msg.get("threshold"), msg.get("ageMs")
    ):
        log.info(
            f"[{device_id}] on-device wake: "
            f"score={msg.get('score')} age={msg.get('ageMs')}ms "
            f"(device triggered)"
        )
    else:
        log.warning(
            f"[{device_id}] malformed oww_wake dropped: {msg!r}"
        )


async def on_listen_state(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    # What the device is DOING with its microphone — the
    # "is it" to oww_local_only's "could it". Sent on every
    # change and after every ack.
    before = device.listen_view
    device.listen_reported = str(msg.get("state") or "") or None
    device.listen_reason   = str(msg.get("reason") or "")
    after = device.listen_view
    if after.private and not before.private:
        # Wake the stream listener off its 10s frame wait
        # so it hands over now: a text payload is read as
        # a stream boundary and it re-checks the mode.
        with contextlib.suppress(asyncio.QueueFull):
            device.mic_queue.put_nowait("listen_mode")
    if after != before:
        log.info(
            f"[{device_id}] listening: {after.state}"
            f"{' (streams continuously)' if after.streams else ''}"
            f"{f' — {after.reason}' if after.reason else ''}"
        )
        if after.state == em_listen.STATE_DEGRADED:
            em_dbwriter.submit(db.log_device, device_id, "warning", "device",
                          f"Wake word unavailable, button only: {after.reason}")
    # #776: a wake word stored OFF, on firmware that
    # cannot stop at the crossing, opens a session on every
    # wake that the controller then closes — while Home
    # Assistant reads "No wake word" throughout. Report
    # "on" so the device works, but KEEP the stored value
    # off: after an upgrade the firmware can honour off
    # again, and the person's choice comes back without them
    # setting it twice. Clearing it would lose that.
    #
    # Here and not on connect: syncListenState is sent
    # AFTER the ack, so at registration listen_reported is
    # still None and a check there would read "not private"
    # for a private Echo — the same bug in a new place.
    if em_wakeword.stored_off_unsupported(
            stored=not device.wake_word_enabled,
            listening_locally=after.state == em_listen.STATE_LOCAL,
            device_can=device.wake_word_off_capable):
        device.wake_word_enabled = True
        device.wake_word_temporarily_on = True
        esphome.update_wake_word(device_id, True, refresh=True)
        em_dbwriter.submit(db.log_device, device_id, "info",
                           "controller",
                           "Wake word reported on temporarily: "
                           "this firmware still sends audio "
                           "on each wake, so the stored off "
                           "is kept until it can honour it")
        log.info(f"[{device_id}] Wake word reported on "
                 f"temporarily — this firmware still sends "
                 f"audio on each wake, so the stored off is "
                 f"kept until it can honour it")
        await em_events.push({
            "type":      "device_update",
            "device_id": device_id,
            "state":     {"wake_word_enabled": True},
        })
    await em_device.push_device_state(device)


async def on_listen_end(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    # The device closed a session itself: a deadline, a
    # mute or a link drop. Its audio has stopped, so the
    # turn will end on its own timers; this just keeps the
    # router honest and says why.
    sess = msg.get("session")
    if isinstance(sess, int) and sess:
        device.listen_router.close(sess)
        if device.listen_session == sess:
            device.listen_session = None
        # #696 review: nothing of this session belongs in a
        # later clip.
        device.wake_capture.reset()
        log.info(f"[{device_id}] session {sess} closed on the device "
                 f"({msg.get('reason')})")


async def on_ble_adverts(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    # BLE proxy data path — batched adverts from the
    # device's passive scanner, forwarded to HA.
    em_ble_proxy.forward_adverts(
        device_id, msg.get("adverts") or []
    )


async def on_wifi_scan_result(device: em_device.Device, msg: dict) -> None:
    fut = device.wifi_scan_future
    if fut is not None and not fut.done():
        fut.set_result(msg)


async def on_log(device: em_device.Device, msg: dict) -> None:
    device_id = device.device_id
    level   = msg.get("level", "info")
    message = msg.get("message", "")
    # em_events.push_log PERSISTS as well as pushing, so this must
    # not also call db.log_device — doing both wrote every
    # device log line twice, ~6ms apart, which is how half the
    # device_logs table came to be duplicates. It also matters
    # for the support bundle: thin_noise keeps the newest three
    # [mem] lines per device, so duplication halved the readings
    # a leak hunt actually gets.
    #
    # The removed call was a synchronous DB write on the event
    # loop; em_events.push_log does it in an executor.
    await em_events.push_log(device_id, level, "device", message)


async def on_pong(device: em_device.Device, msg: dict) -> None:
    loop = asyncio.get_event_loop()
    # Solicited pong (carries our sequence id) -> an RTT
    # sample. Unsolicited keepalive pongs have no id and are
    # ignored here; pairing one with whatever ping happened
    # to be outstanding would invent a measurement.
    _seq = msg.get("id")
    if _seq is not None:
        _sent = device.ping_sent.pop(_seq, None)
        _busy = device.ping_busy.pop(_seq, False)
        if _sent is not None:
            _now = loop.time()
            _rtt = int((_now - _sent) * 1000)
            device.record_rtt(_rtt, _busy)
            device.device_clock.add(_sent, _now, msg.get("mono"))
            if _rtt >= em_device.RTT_EXCURSION_MS:
                # Busy excursions log one for one; idle
                # ones coalesce into a periodic summary.
                _line = device.rtt_log.record(
                    _rtt, _busy, loop.time())
                if _line:
                    log.info(_line)


def note_wear(device, emmc) -> None:
    """Store the day's eMMC wear reading, only when the day or the reading changes."""
    values = em_health.wear_values(emmc)
    if values is None:
        return
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if device.wear_written == (day, values):
        return
    device.wear_written = (day, values)
    em_dbwriter.submit(db.record_wear, device.device_id, day, values)


def _consider_on_device_wake_sample(device, msg: dict, threshold) -> None:
    """Record either kind of on-device crossing through the same capture path."""
    try:
        device.wake_capture.consider(
            enabled=device.wake_clip_capture,
            minimum=device.wake_clip_min_score,
            score=None,
            threshold=float(threshold or device.oww_threshold),
            model=device.oww_model,
            device_score=float(msg.get("score") or 0.0),
            trigger_source="device",
        )
    except (TypeError, ValueError):
        pass
