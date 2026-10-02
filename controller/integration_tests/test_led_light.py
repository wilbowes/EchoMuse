"""ESPHome/voice wiring; run with the full controller dependencies.

python -m unittest discover -s integration_tests -p test_led_light.py -v
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('SERVER_IP', '127.0.0.1')
import em_led_light as light
import em_esphome as esp
from esphome.vendor import api_pb2 as pb


def command(**kw):
    return pb.LightCommandRequest(key=light.LIGHT_KEY, **kw)


def server():
    srv = esp.DeviceESPhomeServer('led-test', 'LED test', '02:00:00:00:00:04',
                                  'test', 0, SimpleNamespace(name='Test', languages=['en']))
    srv.set_capabilities(['leds', 'led_anim'])
    srv.light.sender = AsyncMock()
    return srv


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.srv = server()
        self.sat = self.srv._protocol_factory()
        self.responses = []
        self.sat._send_one = self.responses.append

    async def asyncTearDown(self):
        self.srv.light.release()

    async def test_discovery_is_rgb_and_preserves_other_entity_keys(self):
        self.srv.set_capabilities(['leds', 'led_anim', 'button_hold', 'ambient_light'])
        messages = list(self.sat.handle_message(pb.ListEntitiesRequest()))
        entities = [m for m in messages if not isinstance(m, pb.ListEntitiesDoneResponse)]
        self.assertEqual([m.key for m in entities], [1, 2, 3, 6])
        info = entities[-1]
        self.assertIsInstance(info, pb.ListEntitiesLightResponse)
        self.assertEqual(info.object_id, 'led_ring')
        self.assertEqual(info.name, 'LED Ring')
        self.assertEqual(list(info.supported_color_modes), [pb.COLOR_MODE_RGB])
        self.assertEqual(tuple(info.effects), light.EFFECTS)

    async def test_capability_gate_requires_a_device_side_deadman(self):
        for caps, enabled in [([], False), (['leds'], False), (['led_anim'], False),
                              (['leds', 'led_anim'], True)]:
            self.srv.set_capabilities(caps)
            messages = list(self.sat.handle_message(pb.ListEntitiesRequest()))
            self.assertEqual(any(isinstance(m, pb.ListEntitiesLightResponse)
                                 for m in messages), enabled)
            states = list(self.sat.handle_message(pb.SubscribeStatesRequest()))
            self.assertEqual(any(isinstance(m, pb.LightStateResponse) for m in states), enabled)
            list(self.sat.handle_message(command(has_state=True, state=True)))
            if enabled:
                await asyncio.gather(*self.sat._light_tasks)
            else:
                self.srv.light.sender.assert_not_called()

    async def test_real_protobuf_partial_commands_and_acknowledgement(self):
        list(self.sat.handle_message(command(has_state=True, state=True, has_rgb=True,
                                             red=1, green=0, blue=0.5,
                                             has_brightness=True, brightness=0.25)))
        await asyncio.gather(*self.sat._light_tasks)
        anim = self.srv.light.sender.call_args.args[0]
        self.assertEqual(anim['colors'], [[64, 0, 32]])
        self.assertEqual(anim['pattern'], 'solid')
        self.assertEqual(anim['ttlSec'], 60)
        self.assertTrue(self.responses[-1].state)
        self.assertEqual(self.responses[-1].key, 6)
        await self.sat._light_command(command(has_effect=True, effect='Pulse'))
        self.assertEqual(self.responses[-1].effect, 'Pulse')
        self.assertEqual(self.responses[-1].brightness, 0.25)
        await self.sat._light_command(command(has_state=True, state=False))
        self.assertFalse(self.responses[-1].state)
        self.assertEqual(self.srv.light.sender.call_args.args[0]['pattern'], 'off')

    async def test_unsupported_commands_and_write_failures_report_last_accepted_state(self):
        await self.sat._light_command(command(has_state=True, state=True))
        initial = self.srv.light.state
        for fields in [dict(has_brightness=True, brightness=float('nan')),
                       dict(has_rgb=True, red=float('inf')),
                       dict(has_color_mode=True, color_mode=pb.COLOR_MODE_WHITE),
                       dict(has_flash_length=True, flash_length=100),
                       dict(has_effect=True, effect='Meter')]:
            await self.sat._light_command(command(**fields))
            self.assertEqual(self.srv.light.state, initial)
            self.assertTrue(self.responses[-1].state)
        self.srv.light.sender.side_effect = OSError('closed')
        await self.sat._light_command(command(has_state=True, state=False))
        self.assertEqual(self.srv.light.state, initial)
        self.assertTrue(self.responses[-1].state)

    async def test_unknown_key_and_subdevice_are_ignored(self):
        for field, value in [('key', 4), ('key', 5), ('key', 99), ('device_id', 1)]:
            msg = command(has_state=True, state=True)
            setattr(msg, field, value)
            list(self.sat.handle_message(msg))
        self.assertFalse(self.sat._light_tasks)
        self.srv.light.sender.assert_not_called()

    async def test_transitions_are_immediate_and_do_not_stream_frames(self):
        await self.sat._light_command(command(has_state=True, state=True,
                                               has_transition_length=True, transition_length=1000))
        self.srv.light.sender.assert_awaited_once()
        self.assertEqual(self.srv.light.sender.call_args.args[0]['pattern'], 'solid')

    async def test_expiry_is_published_on_the_active_connection(self):
        self.srv.light.clock = lambda: 100
        await self.sat._light_command(command(has_state=True, state=True))
        self.srv.light.clock = lambda: 160
        self.srv.light._expire()
        self.assertIsInstance(self.responses[-1], pb.LightStateResponse)
        self.assertFalse(self.responses[-1].state)
        self.srv.light.sender.assert_awaited_once()

    async def test_ha_disconnect_stops_renewal_and_reconnect_resumes_it(self):
        self.srv.light.clock = lambda: 100
        await self.sat._light_command(command(has_state=True, state=True))
        deadline = self.srv.light.expires_at
        task = self.srv.light._renew_task
        self.srv._on_satellite_disconnected(self.sat)
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.srv.light.clock = lambda: 120
        await self.srv.light.renew()
        self.srv.light.sender.assert_awaited_once()
        self.assertEqual(self.srv.light.expires_at, deadline)
        new = self.srv._protocol_factory()
        new._send_one = self.responses.append
        states = list(new.handle_message(pb.SubscribeStatesRequest()))
        self.assertTrue(next(m for m in states if isinstance(m, pb.LightStateResponse)).state)
        await asyncio.sleep(0)
        self.assertEqual(self.srv.light.sender.await_count, 2)
        self.assertEqual(self.srv.light.expires_at, 180)
        renewal = self.srv.light._renew_task
        self.srv._on_satellite_disconnected(self.sat)
        self.assertIs(self.srv.light._renew_task, renewal)
        await self.sat._light_command(command(has_state=True, state=False))
        self.assertTrue(self.srv.light.state.state)
        self.srv._on_satellite_disconnected(new)
        self.srv.light.clock = lambda: 180
        self.srv.light._expire()
        newest = self.srv._protocol_factory()
        states = list(newest.handle_message(pb.SubscribeStatesRequest()))
        self.assertFalse(next(m for m in states if isinstance(m, pb.LightStateResponse)).state)
        self.assertIsNone(self.srv.light._renew_task)

    async def test_ha_disconnect_cancels_queued_command(self):
        await self.srv.light.lock.acquire()
        list(self.sat.handle_message(command(has_state=True, state=True)))
        task = next(iter(self.sat._light_tasks))
        await asyncio.sleep(0)
        self.srv._on_satellite_disconnected(self.sat)
        self.srv.light.lock.release()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertFalse(self.srv.light.state.state)
        self.srv.light.sender.assert_not_called()

    async def test_device_disconnect_and_reconnect_forget_manual_state(self):
        await self.srv.light.command(state=True)
        self.srv.light.suspend()
        self.srv.light.schedule_restore(delay=10)
        task = self.srv.light._restore_task
        expiry = self.srv.light._expiry_handle
        with patch.dict(esp._servers, {self.srv.device_id: self.srv}):
            await esp.device_disconnected(self.srv.device_id)
            self.assertFalse(self.srv.light.state.state)
            self.assertIsNone(self.srv.light.sender)
            self.srv.start = AsyncMock()
            await esp.device_connected(self.srv.device_id,
                                        send_led_ring_animation=AsyncMock())
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertTrue(expiry.cancelled())
        self.assertFalse(self.srv.light.state.state)
        self.assertIsNone(self.srv.light.expires_at)
        self.assertFalse(self.responses[-1].state)

    async def test_server_shutdown_cleans_up_manual_light(self):
        await self.srv.light.command(state=True)
        self.srv.light.suspend()
        self.srv.light.schedule_restore(delay=10)
        task = self.srv.light._restore_task
        await self.srv.stop()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.srv.light.sender)
        self.assertFalse(self.srv.light.state.state)


class VoiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_controller_voice_cleanup_restores_requested_settings(self):
        import em_controller as ctl
        for scenario in ('normal', 'pattern', 'off_during_turn', 'colour_during_turn',
                         'continuation', 'barge', 'error', 'early_error', 'cancel', 'long_turn'):
            with self.subTest(scenario=scenario):
                srv = server()
                ws = SimpleNamespace(send=AsyncMock())
                device = ctl.Device(srv.device_id, '127.0.0.1', ['leds', 'led_anim'], ws)
                ring = srv.light
                now = 100
                ring.clock = lambda: now
                ring.sender = lambda anim: light.send_animation_to_device(device, anim)
                ring.ready = lambda: not (device.voice_lock.locked() or
                                           device.timer_alarm_ringing or device.muted)
                ring.set_connected(True)
                await ring.command(state=True, red=1, green=0, blue=0, brightness=0.4,
                                   effect='Spin' if scenario == 'pattern' else 'None')
                initial_state = ring.state
                calls = 0

                async def run_turn(**kwargs):
                    nonlocal calls, now
                    calls += 1
                    self.assertTrue(ring.suspended)
                    self.assertTrue(device.voice_lock.locked())
                    if scenario == 'off_during_turn':
                        await ring.command(state=False)
                    if scenario == 'colour_during_turn':
                        await ring.command(red=0, green=0, blue=1)
                    if scenario == 'long_turn':
                        for _ in range(4):
                            now += 30
                            await ring.renew()
                    if scenario == 'barge' and calls == 1:
                        device.barge_detected = True
                    if scenario == 'error':
                        raise OSError('pipeline failed')
                    if scenario == 'cancel':
                        raise asyncio.CancelledError()
                    return scenario == 'continuation' and calls == 1

                try:
                    with patch.dict(esp._servers, {srv.device_id: srv}), \
                         patch.object(esp, 'trigger_voice_turn', side_effect=run_turn), \
                         patch.object(ctl, '_push_device_state', new=AsyncMock(
                             side_effect=RuntimeError('early failure') if scenario == 'early_error' else None)), \
                         patch.object(ctl.em_player, 'interrupt', new=AsyncMock()), \
                         patch.object(ctl.em_player, 'resume_interrupted', new=AsyncMock()):
                        if scenario in ('error', 'early_error', 'cancel'):
                            with self.assertRaises((OSError, RuntimeError, asyncio.CancelledError)):
                                await ctl._run_voice_locked(device, is_wakeword=True)
                        else:
                            await ctl._run_voice_locked(device, is_wakeword=True)
                        self.assertIsNotNone(ring._restore_task)
                        await asyncio.wait_for(ring._restore_task, 1)
                    self.assertFalse(ring.suspended)
                    self.assertEqual(calls, 2 if scenario in ('continuation', 'barge') else
                                     (0 if scenario == 'early_error' else 1))
                    animations = [json.loads(c.args[0])['anim'] for c in ws.send.call_args_list
                                  if json.loads(c.args[0])['type'] == 'led_anim']
                    last = animations[-1]
                    if scenario == 'off_during_turn':
                        self.assertFalse(ring.state.state)
                        self.assertEqual(last['pattern'], 'off')
                    elif scenario == 'colour_during_turn':
                        self.assertEqual(last['colors'], [[0, 0, 102]])
                    else:
                        self.assertEqual(ring.state, initial_state)
                        self.assertEqual(last, initial_state.animation())
                finally:
                    ring.release()


if __name__ == '__main__':
    unittest.main()
