# Home Assistant LED ring indicator

EchoMuse exposes **LED Ring** as an RGB light through each device's existing
ESPHome integration. It is an idle status indicator for notifications and
household information. Devices must advertise both `leds` and `led_anim`;
older firmware keeps its normal status ring but does not expose this light.
No separate integration or controller option is required. Reload the device's
ESPHome integration after updating the controller to discover the new entity.

The light supports on/off, brightness, RGB colour and these effects:

| Effect | Device animation | Appearance |
|---|---|---|
| None | `solid` | Solid selected colour |
| Spin / Slow spin | `spin` | Selected colour with a dim trail |
| Rotate | `rotate` | A three-LED arc in the selected colour |
| Pulse / Breathe | `pulse` | Selected colour pulsing over 1.2 / 3 seconds |
| Rainbow | `rotate` | Rotating rainbow; brightness applies, RGB is ignored |

Meter is omitted because the firmware measures voice-response audio before
mixing in music. The normal voice-response meter remains unchanged.

Every effect runs on the device's existing animation engine. Home Assistant
sends a pattern, not a stream of frames. Brightness and colour brightness scale
RGB directly; there is no additional colour calibration. Transitions apply
immediately. For repeated flashing use Pulse; the light does not support HA's
one-shot `flash` parameter.

## Lifetime and priority

**The light stays on until HA turns it off.** Every device pattern, including
solid colours, has a 60-second `ttlSec`. The controller resends the setting
every 30 seconds while the light is on and HA is connected. No renewal automation
is needed. Renewal stops when HA disconnects; if HA or the controller goes away,
the device clears the indicator within a minute of the last renewal.

Listening, thinking, response playback, outcome cues and timer alarms take the
ring immediately. Microphone mute and the physical volume arc remain owned by
the firmware. The requested light setting resumes with a fresh 60-second TTL
after those indications end, even after a long voice turn. Renewal preserves the
pending setting during an override without writing over the status indication.
HA changes made during an override update the pending setting; an off command
cancels it without clearing the higher-priority indication.

The HA entity reports the requested ambient setting, including while a status
indication temporarily covers it. It is not LED hardware readback: the existing
protocol has no display acknowledgement. Failed socket writes retain the last
accepted state. Device reconnects and controller restarts clear the setting;
an HA reconnect resumes renewal if the previous lease has not yet expired.
An expired setting stays off until HA turns it on again.

## Example: keep an alarm indicator current

Replace the example entities with your helper and EchoMuse light. This updates
immediately when the helper changes. The controller renews it while active.
Turning the helper off clears it immediately; if HA stops, it expires on-device.

```yaml
alias: EchoMuse idle alarm indicator
triggers:
  - trigger: state
    entity_id: input_boolean.alarm_indicator
  - trigger: homeassistant
    event: start
actions:
  - choose:
      - conditions:
          - condition: state
            entity_id: input_boolean.alarm_indicator
            state: "on"
        sequence:
          - action: light.turn_on
            target:
              entity_id: light.garage_led_ring
            data:
              rgb_color: [255, 0, 80]
              brightness_pct: 30
              effect: Spin
    default:
      - action: light.turn_off
        target:
          entity_id: light.garage_led_ring
mode: restart
```

For a temporary notification, call `light.turn_on`, then call `light.turn_off`
after the desired delay.
