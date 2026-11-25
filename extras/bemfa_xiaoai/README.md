# Optional: Bemfa cloud → Mijia / Xiaoai

Bemfa (巴法云) is a Chinese IoT cloud that offers an official Mijia (米家) platform
integration. With this small HA custom component you can mirror HA entities to Bemfa
topics, and the Mijia app / Xiaoai speakers can control them.

This is what was used to give the 6 indoor units and one light switch voice control.

## How it works

- Each device is a Bemfa MQTT topic; the **last three digits of the topic select the
  device type**: `005` = air conditioner, `006` = switch.
- The topic for an entity is `hass` + `md5(entity_id)` + suffix (same scheme as the
  official `bemfa` HACS add-on, so topics stay stable).
- The component embeds a minimal MQTT 3.1.1 client (plain sockets, no `paho-mqtt`
  dependency): it subscribes to the topics, translates incoming messages into HA service
  calls, and publishes HA state changes to `<topic>/set`.
- Bemfa echoes your own publications back; the component drops messages that equal the
  last published payload to avoid self-triggered loops.

## Mode mapping (configurable in `bridge.py`)

| Mijia / Xiaoai | Unit mode |
|---|---|
| cool (制冷) | cool (冷风) |
| heat (制热) | **heat + floor** (热+暖) |
| auto (自动) | **floor heating** (地暖) |
| fan (送风) | fan only |
| dry (除湿) | dry |

The mapping is the owner's preference (the unit has more modes than Mijia exposes).

## Install

1. Copy `custom_components/bemfa_xiaoai` to `/config/custom_components/`.
2. Create the topics on the Bemfa console (or with its HTTP API) so they exist before the
   Mijia app imports them; topic names must end with `005` / `006`.
3. Add to `configuration.yaml`:

   ```yaml
   bemfa_xiaoai:
     uid: !secret bemfa_uid
     entities:
       - switch.yard_light
       - climate.living_room
   ```

4. Restart HA. Logs should show a successful MQTT connection, and the topics should show
   as online in the Bemfa console.
5. Mijia app → 我的 → 其他平台设备 → 添加 → 巴法 → sign in with the Bemfa account.
   Devices are imported automatically.

## Notes

- Commands are applied one at a time; a mode change is followed by a 2 s pause and a
  verified temperature write (the controller drops setpoints that arrive too early).
- Cloud push latency is 1–2 s for switches and ~10–15 s for AC units (controller reaction
  included).
