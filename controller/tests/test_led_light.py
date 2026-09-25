"""Issue #66: idle-only native animations and a bounded HA lease.

This suite imports only the standard library and em_led_light; it runs in the
same minimal environment as the rest of the controller's CI tests.
"""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import em_led_light as light


class StateTests(unittest.TestCase):
    def test_rgb_brightness_and_partial_updates(self):
        state = light.RingState().update(state=True, brightness=0.5,
                                        red=1, green=0, blue=0.25)
        self.assertEqual(state.animation()['colors'], [[128, 0, 32]])
        dim = state.update(color_brightness=0.5)
        self.assertEqual(dim.animation()['colors'], [[64, 0, 16]])
        self.assertEqual(dim.update(state=False).animation()['pattern'], 'off')
        self.assertEqual(dim.update(state=False).update(state=True), dim)

    def test_all_native_patterns_have_ttl_and_never_claim_listening(self):
        expected = {'None': 'solid', 'Spin': 'spin', 'Slow spin': 'spin',
                    'Rotate': 'rotate', 'Pulse': 'pulse', 'Breathe': 'pulse',
                    'Rainbow': 'rotate'}
        self.assertEqual(set(light.EFFECTS), set(expected))
        for effect, pattern in expected.items():
            with self.subTest(effect=effect):
                anim = light.RingState(state=True, effect=effect).animation()
                self.assertEqual(anim['pattern'], pattern)
                self.assertGreater(anim['ttlSec'], 0)
                self.assertLessEqual(anim['ttlSec'], 60)
                self.assertIs(anim['listening'], False)
        self.assertEqual(light.RingState(state=True, effect='Pulse').animation()['periodMs'], 1200)
        self.assertEqual(light.RingState(state=True, effect='Breathe').animation()['periodMs'], 3000)

    def test_rainbow_palette_obeys_brightness(self):
        anim = light.RingState(state=True, effect='Rainbow', brightness=0.5).animation()
        self.assertEqual(len(anim['colors']), 12)
        self.assertEqual([anim['colors'][i] for i in (0, 4, 8)],
                         [[128, 0, 0], [0, 128, 0], [0, 0, 128]])

    def test_invalid_values_and_finite_clamping(self):
        for field in ('brightness', 'color_brightness', 'red', 'green', 'blue'):
            self.assertEqual(getattr(light.RingState().update(**{field: 2}), field), 1)
            self.assertEqual(getattr(light.RingState().update(**{field: -1}), field), 0)
            for value in (float('nan'), float('inf'), -float('inf')):
                with self.assertRaises(ValueError):
                    light.RingState().update(**{field: value})
        for effect in ('not an effect', 'Meter'):
            with self.assertRaises(ValueError):
                light.RingState().update(effect=effect)
        self.assertEqual(light.RingState().update(effect='').effect, 'None')
        with self.assertRaises(ValueError):
            light.RingState(state=True).animation(ttl=0)


class RingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.updates = []
        self.ring = light.RingLight(clock=lambda: self.now, on_change=self.updates.append)
        self.ring.sender = AsyncMock()
        self.ring.set_connected(True)

    async def asyncTearDown(self):
        self.ring.release()

    async def test_solid_on_off_and_effect_replacement_use_only_native_specs(self):
        await self.ring.command(state=True, red=1, green=0, blue=0.25, brightness=0.5)
        anim = self.ring.sender.call_args.args[0]
        self.assertEqual(anim['pattern'], 'solid')
        self.assertEqual(anim['colors'], [[128, 0, 32]])
        await self.ring.command(effect='Spin')
        self.assertEqual(self.ring.sender.call_args.args[0]['pattern'], 'spin')
        await self.ring.command(state=False)
        self.assertEqual(self.ring.sender.call_args.args[0]['pattern'], 'off')
        self.assertIsNone(self.ring.expires_at)
        self.assertIsNone(self.ring._renew_task)
        await self.ring.command(state=True)
        self.assertEqual(self.ring.sender.call_args.args[0]['pattern'], 'spin')

    async def test_failure_does_not_acknowledge_or_renew(self):
        await self.ring.command(state=True)
        before, deadline = self.ring.state, self.ring.expires_at
        self.now += 10
        for sender in (None, AsyncMock(side_effect=OSError('closed'))):
            self.ring.sender = sender
            with self.assertRaises((RuntimeError, OSError)):
                await self.ring.command(brightness=0.5)
            self.assertEqual(self.ring.state, before)
            self.assertEqual(self.ring.expires_at, deadline)

    async def test_expiry_updates_ha_without_a_network_off(self):
        await self.ring.command(state=True, effect='Spin')
        self.ring.set_connected(False)
        self.now += 60
        self.ring._expire()
        self.assertFalse(self.ring.state.state)
        self.assertIsNone(self.ring.expires_at)
        self.assertEqual(self.updates[-1], self.ring.state)
        self.ring.sender.assert_awaited_once()

    async def test_new_command_renews_but_empty_command_does_not(self):
        await self.ring.command(state=True)
        first_timer = self.ring._expiry_handle
        self.now += 30
        await self.ring.command(brightness=0.5)
        self.assertTrue(first_timer.cancelled())
        self.assertEqual(self.ring.expires_at, 190)
        self.now += 10
        await self.ring.command()
        self.assertEqual(self.ring.expires_at, 190)
        self.assertEqual(self.ring.sender.await_count, 2)

    async def test_controller_renews_every_30_seconds_until_off(self):
        self.assertEqual(light.RENEW_SECONDS, 30)
        renewed = asyncio.Event()
        frames = []

        async def send(anim):
            frames.append(anim)
            self.now += 30
            if len(frames) == 4:
                renewed.set()

        self.ring.sender = send
        with patch.object(light, 'RENEW_SECONDS', 0.01):
            await self.ring.command(state=True, effect='Rainbow', brightness=0.4)
            await asyncio.wait_for(renewed.wait(), 1)
            self.assertTrue(self.ring.state.state)
            self.assertEqual(self.ring.expires_at, self.now + 60)
            self.assertEqual(frames, [self.ring.state.animation()] * 4)
            task = self.ring._renew_task
            await self.ring.command(state=False)
            await asyncio.sleep(0)
            self.assertTrue(task.cancelled())
            self.assertEqual(frames[-1]['pattern'], 'off')

    async def test_ha_disconnect_stops_renewal_and_restore_until_expiry(self):
        await self.ring.command(state=True)
        task = self.ring._renew_task
        self.ring.suspend()
        self.ring.set_connected(False)
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.now += 30
        await self.ring.renew()
        self.assertFalse(await self.ring.restore())
        self.ring.sender.assert_awaited_once()
        self.assertEqual(self.ring.expires_at, 160)
        self.now += 30
        self.ring._expire()
        self.assertFalse(self.ring.state.state)

    async def test_renewal_failure_keeps_deadline_and_retries(self):
        retried = asyncio.Event()
        calls = 0

        async def send(anim):
            nonlocal calls
            calls += 1
            self.now += 10
            if calls == 2:
                raise OSError('temporary failure')
            if calls == 3:
                retried.set()

        self.ring.sender = send
        with patch.object(light, 'RENEW_SECONDS', 0.01):
            with self.assertLogs(light.log, level='ERROR'):
                await self.ring.command(state=True)
                await asyncio.wait_for(retried.wait(), 1)
        self.assertTrue(self.ring.state.state)
        self.assertEqual(self.ring.expires_at, self.now + 60)

    async def test_renewal_and_commands_are_serialised(self):
        await self.ring.command(state=True)
        entered, finish = asyncio.Event(), asyncio.Event()
        frames = []

        async def send(anim):
            if not frames:
                entered.set()
                await finish.wait()
            frames.append(anim)

        self.ring.sender = send
        task = asyncio.create_task(self.ring.renew())
        await entered.wait()
        off = asyncio.create_task(self.ring.command(state=False))
        await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(task, off)
        self.assertEqual([a['pattern'] for a in frames], ['solid', 'off'])
        await self.ring.renew()
        self.assertEqual(len(frames), 2)

    async def test_ha_disconnect_cancels_inflight_renewal(self):
        await self.ring.command(state=True)
        entered = asyncio.Event()

        async def send(anim):
            entered.set()
            await asyncio.Event().wait()

        self.ring.sender = send
        self.ring.set_connected(False)
        self.ring.set_connected(True)
        task = self.ring._renew_task
        await entered.wait()
        self.ring.set_connected(False)
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.ring.expires_at, 160)

    async def test_queued_renewal_cannot_cross_ha_reconnect(self):
        await self.ring.command(state=True)
        await self.ring.lock.acquire()
        task = asyncio.create_task(self.ring.renew())
        await asyncio.sleep(0)
        self.ring.set_connected(False)
        self.ring.set_connected(True)
        self.ring.lock.release()
        await task
        await asyncio.sleep(0)
        self.assertEqual(self.ring.sender.await_count, 2)

    async def test_suspension_keeps_latest_command_without_painting(self):
        await self.ring.command(state=True)
        self.ring.suspend()
        self.ring.sender.reset_mock()
        self.ring.ready = lambda: False
        await self.ring.command(effect='Breathe', brightness=0.3, red=1, green=0, blue=0)
        self.ring.sender.assert_not_called()
        self.assertFalse(await self.ring.restore())
        self.ring.ready = lambda: True
        self.now += 20
        self.assertTrue(await self.ring.restore())
        anim = self.ring.sender.call_args.args[0]
        self.assertEqual(anim['pattern'], 'pulse')
        self.assertEqual(anim['colors'], [[76, 0, 0]])
        self.assertEqual(anim['ttlSec'], 60)
        self.assertEqual(self.ring.expires_at, 180)

    async def test_suspended_pattern_survives_a_long_turn_without_painting(self):
        await self.ring.command(state=True)
        self.ring.suspend()
        self.ring.sender.reset_mock()
        self.ring.ready = lambda: False
        for _ in range(4):
            self.now += 30
            await self.ring.renew()
            self.assertEqual(self.ring.expires_at, self.now + 60)
        self.ring.sender.assert_not_called()
        self.ring.ready = lambda: True
        self.assertTrue(await self.ring.restore())
        self.ring.sender.assert_awaited_once_with(self.ring.state.animation())
        self.assertTrue(self.ring.state.state)
        self.assertFalse(self.ring.suspended)

    async def test_reconnect_after_expiry_does_not_replay_the_setting(self):
        await self.ring.command(state=True)
        self.ring.suspend()
        self.ring.set_connected(False)
        self.now += 60
        self.ring.sender.reset_mock()
        self.ring.set_connected(True)
        await self.ring.restore()
        self.ring.sender.assert_not_called()
        self.assertFalse(self.ring.state.state)

    async def test_off_during_overlay_does_not_clear_status_ring(self):
        await self.ring.command(state=True)
        self.ring.suspend()
        self.ring.sender.reset_mock()
        await self.ring.command(state=False)
        await self.ring.restore()
        self.ring.sender.assert_not_called()
        self.assertFalse(self.ring.state.state)

    async def test_busy_device_refuses_unsuspended_command(self):
        self.ring.ready = lambda: False
        with self.assertRaises(RuntimeError):
            await self.ring.command(state=True)
        self.ring.sender.assert_not_called()
        self.assertFalse(self.ring.state.state)

    async def test_reset_during_send_cannot_commit_stale_state(self):
        async def disconnect(_):
            self.ring.release()
        self.ring.sender = disconnect
        await self.ring.command(state=True)
        self.assertFalse(self.ring.state.state)
        self.assertIsNone(self.ring.expires_at)

    async def test_queued_command_cannot_cross_a_device_reconnect(self):
        await self.ring.lock.acquire()
        task = asyncio.create_task(self.ring.command(state=True))
        await asyncio.sleep(0)
        self.ring.release()
        self.ring.lock.release()
        with self.assertRaises(RuntimeError):
            await task
        self.ring.sender.assert_not_called()

    async def test_commands_are_serialised(self):
        frames = []
        async def send(anim):
            await asyncio.sleep(0)
            frames.append(anim)
        self.ring.sender = send
        await asyncio.gather(self.ring.command(state=True),
                             self.ring.command(brightness=0.5),
                             self.ring.command(state=False))
        self.assertEqual([a['pattern'] for a in frames], ['solid', 'solid', 'off'])
        self.assertEqual(frames[1]['colors'], [[128, 128, 128]])

    async def test_new_status_cancels_old_restore(self):
        await self.ring.command(state=True)
        self.ring.sender.reset_mock()
        self.ring.suspend()
        self.ring.schedule_restore(delay=10)
        task = self.ring._restore_task
        self.ring.suspend()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.ring.sender.assert_not_called()
        self.ring.schedule_restore()
        await asyncio.wait_for(self.ring._restore_task, 1)
        self.ring.sender.assert_awaited_once()

    async def test_restore_waits_for_idle_and_preserves_outcome_delay(self):
        await self.ring.command(state=True)
        self.ring.sender.reset_mock()
        self.ring.ready = lambda: False
        self.ring.suspend()
        self.ring.schedule_restore(delay=0.02)
        task = self.ring._restore_task
        self.ring.schedule_restore()
        self.assertIs(self.ring._restore_task, task)
        await asyncio.sleep(0.03)
        self.ring.sender.assert_not_called()
        self.ring.ready = lambda: True
        await asyncio.wait_for(task, 1)
        self.ring.sender.assert_awaited_once()

    async def test_ha_reconnect_preserves_pending_outcome_delay(self):
        await self.ring.command(state=True)
        self.ring.sender.reset_mock()
        self.ring.suspend()
        self.ring.schedule_restore(delay=0.05)
        task = self.ring._restore_task
        self.ring.set_connected(False)
        self.ring.set_connected(True)
        self.assertIs(self.ring._restore_task, task)
        await asyncio.sleep(0.01)
        self.ring.sender.assert_not_called()
        await asyncio.wait_for(task, 1)
        self.ring.sender.assert_awaited_once()

    async def test_disconnect_cancels_expiry_and_restore(self):
        await self.ring.command(state=True)
        expiry = self.ring._expiry_handle
        renewal = self.ring._renew_task
        self.ring.suspend()
        self.ring.schedule_restore(delay=10)
        task = self.ring._restore_task
        self.ring.release()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertTrue(expiry.cancelled())
        self.assertTrue(renewal.cancelled())
        self.assertFalse(self.ring.state.state)


class DeviceCallbackTests(unittest.IsolatedAsyncioTestCase):
    def device(self):
        return SimpleNamespace(voice_lock=asyncio.Lock(), timer_alarm_ringing=False,
                               muted=False, led_anim_capable=True,
                               control_ws=SimpleNamespace(send=AsyncMock()))

    async def test_native_wire_message_and_failure_propagation(self):
        device = self.device()
        anim = light.RingState(state=True).animation()
        await light.send_animation_to_device(device, anim)
        self.assertEqual(json.loads(device.control_ws.send.call_args.args[0]),
                         {'type': 'led_anim', 'anim': anim})
        device.control_ws.send.side_effect = OSError('closed')
        with self.assertRaises(OSError):
            await light.send_animation_to_device(device, anim)
        self.assertFalse(device.voice_lock.locked())

    async def test_mute_timer_voice_and_old_firmware_refuse_writes(self):
        device = self.device()
        anim = light.RingState(state=True).animation()
        for flag in ('muted', 'timer_alarm_ringing'):
            setattr(device, flag, True)
            with self.assertRaises(RuntimeError):
                await light.send_animation_to_device(device, anim)
            setattr(device, flag, False)
        async with device.voice_lock:
            with self.assertRaises(RuntimeError):
                await light.send_animation_to_device(device, anim)
        device.led_anim_capable = False
        with self.assertRaises(RuntimeError):
            await light.send_animation_to_device(device, anim)
        device.control_ws.send.assert_not_called()

    async def test_slow_light_write_does_not_look_like_a_running_turn(self):
        device = self.device()
        entered, finish = asyncio.Event(), asyncio.Event()
        order = []
        async def send(_):
            entered.set()
            await finish.wait()
            order.append('manual')
        device.control_ws.send = send
        task = asyncio.create_task(light.send_animation_to_device(
            device, light.RingState(state=True).animation()))
        await entered.wait()
        self.assertFalse(device.voice_lock.locked())
        async def turn():
            async with device.voice_lock:
                order.append('listening')
        voice = asyncio.create_task(turn())
        await asyncio.wait_for(voice, 1)
        self.assertFalse(task.done())
        finish.set()
        await asyncio.gather(task, voice)
        self.assertEqual(order, ['listening', 'manual'])
