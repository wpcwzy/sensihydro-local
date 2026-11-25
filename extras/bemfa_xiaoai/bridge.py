"""Sync a few Home Assistant entities with the Bemfa cloud (Xiaomi/Xiaoai).

Bemfa devices are plain MQTT topics; the last three characters of the topic
select the device type (005 = air conditioner, 006 = switch).  Xiaomi's
Mijia app can import a Bemfa account, which makes these devices controllable
from the Mijia app and from Xiaoai voice assistants.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback

from .mqtt import MqttClient

_LOGGER = logging.getLogger(__name__)

MQTT_HOST = "bemfa.com"
MQTT_PORT = 9501
SUFFIX_CLIMATE = "005"
SUFFIX_SWITCH = "006"

# Bemfa (Mijia/Xiaoai) air-conditioner modes: 1 auto, 2 cool, 3 heat,
# 4 fan only, 5 dry.
#
# Mapping requested by the owner:
#   制热 (3) -> 热+暖   (HA heat_cool)
#   自动 (1) -> 地暖    (preset 地暖)
# The unit's own mode name (climate attribute ``mode_name``) disambiguates
# 热风 / 地暖 / 热+暖, which all map to the same HA hvac mode otherwise.
MODE_NAME_TO_BEMFA = {
    "冷风": 2,
    "送风": 4,
    "热风": 3,
    "热+暖": 3,
    "地暖": 1,
    "除湿": 5,
}
BEMFA_TO_MODE = {1: "heat_cool", 2: "cool", 3: "heat", 4: "fan_only", 5: "dry"}
PRESET_FLOOR = "地暖"
MIN_TEMP = 5.0
MAX_TEMP = 32.0


def generate_topic(entity_id: str) -> str:
    """Return the Bemfa topic for an entity (same scheme as the bemfa add-on)."""
    suffix = SUFFIX_CLIMATE if entity_id.startswith("climate.") else SUFFIX_SWITCH
    return "hass" + hashlib.md5(entity_id.encode("utf-8")).hexdigest() + suffix


class BemfaBridge:
    """Keep HA entity states and Bemfa topics in sync."""

    def __init__(self, hass: HomeAssistant, uid: str, entities: list[str]) -> None:
        self.hass = hass
        self.uid = uid
        self.entities = [e for e in entities if e.startswith(("switch.", "climate."))]
        self.entity_topic = {e: generate_topic(e) for e in self.entities}
        self.topic_entity = {t: e for e, t in self.entity_topic.items()}
        self.client = MqttClient(MQTT_HOST, MQTT_PORT, uid, keepalive=60)
        self.client.on_message = self._on_mqtt_message
        self.client.on_connected = self._on_mqtt_connected
        self._unsub: list = []
        self._last_payload: dict[str, str] = {}
        # Commands are applied one at a time so two quick voice commands do not
        # interleave their writes on the Modbus bus.
        self._apply_lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._unsub.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_listener)
        )
        self._unsub.append(
            self.hass.bus.async_listen_once("homeassistant_stop", lambda _e: self.stop())
        )
        _LOGGER.info(
            "Bemfa sync started for %d entities as %s", len(self.entities), self.uid
        )
        self.client.start()

    def stop(self) -> None:
        self.client.stop()
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    # ------------------------------------------------------------ HA -> bemfa

    @callback
    def _state_listener(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id in self.entity_topic:
            self._publish_state(entity_id)

    @callback
    def _on_mqtt_connected(self) -> None:
        # Called from the MQTT thread: schedule subscription + full state push.
        self.hass.loop.call_soon_threadsafe(self._publish_all)

    @callback
    def _publish_all(self) -> None:
        self.client.subscribe(list(self.topic_entity))
        self._last_payload.clear()
        for entity_id in self.entities:
            self._publish_state(entity_id)

    @callback
    def _publish_state(self, entity_id: str) -> None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return
        if entity_id.startswith("switch."):
            payload = "on" if state.state == "on" else "off"
        elif state.state == "off":
            payload = "off"
        else:
            attributes = state.attributes
            mode_name = attributes.get("mode_name")
            mode = MODE_NAME_TO_BEMFA.get(mode_name)
            if mode is None:
                mode = {"cool": 2, "heat": 3, "heat_cool": 3, "fan_only": 4, "dry": 5}.get(
                    state.state, 1
                )
            temperature = attributes.get("temperature")
            if temperature is None:
                payload = f"on#{mode}"
            else:
                payload = f"on#{mode}#{round(float(temperature))}"
        if self._last_payload.get(entity_id) == payload:
            return
        self._last_payload[entity_id] = payload
        self.client.publish(self.entity_topic[entity_id] + "/set", payload)

    # ------------------------------------------------------------ bemfa -> HA

    def _on_mqtt_message(self, topic: str, payload: str) -> None:
        entity_id = self.topic_entity.get(topic)
        if entity_id is None:
            return
        payload = payload.strip()
        # Bemfa echoes our own publications back to us: ignore messages that
        # match the state we last pushed (otherwise we would loop on them).
        if payload == self._last_payload.get(entity_id):
            return
        _LOGGER.info("Bemfa command for %s: %s", entity_id, payload)
        self.hass.loop.call_soon_threadsafe(
            self.hass.async_create_task, self._async_apply(entity_id, payload)
        )

    async def _async_apply(self, entity_id: str, payload: str) -> None:
        async with self._apply_lock:
            await self._async_apply_locked(entity_id, payload)

    async def _async_apply_locked(self, entity_id: str, payload: str) -> None:
        if entity_id.startswith("switch."):
            if payload == "on":
                await self._call("switch", "turn_on", entity_id)
            elif payload == "off":
                await self._call("switch", "turn_off", entity_id)
            return

        parts = payload.split("#")
        if parts[0] == "off":
            await self._call("climate", "turn_off", entity_id)
            return
        if parts[0] != "on":
            return
        if len(parts) == 1:
            await self._call("climate", "turn_on", entity_id)
            return

        mode_changed = False
        if len(parts) > 1 and parts[1]:
            try:
                bemfa_mode = int(parts[1])
            except ValueError:
                bemfa_mode = None
            if bemfa_mode == 1:
                # 自动 -> 地暖
                await self._call("climate", "turn_on", entity_id)
                await self._call(
                    "climate", "set_preset_mode", entity_id, {"preset_mode": PRESET_FLOOR}
                )
                mode_changed = True
            elif bemfa_mode == 3:
                # 制热 -> 热+暖
                await self._call(
                    "climate", "set_hvac_mode", entity_id, {"hvac_mode": "heat_cool"}
                )
                mode_changed = True
            elif bemfa_mode is not None:
                mode = BEMFA_TO_MODE.get(bemfa_mode)
                if mode is not None:
                    await self._call(
                        "climate", "set_hvac_mode", entity_id, {"hvac_mode": mode}
                    )
                    mode_changed = True
            if mode_changed:
                # The unit controller ignores a setpoint write that arrives while
                # a mode change is still being applied, so give it a moment.
                await asyncio.sleep(2.0)

        if len(parts) > 2 and parts[2]:
            try:
                temperature = float(parts[2])
            except ValueError:
                temperature = None
            if temperature is not None:
                temperature = max(MIN_TEMP, min(MAX_TEMP, temperature))
                for _attempt in range(3):
                    await self._call(
                        "climate", "set_temperature", entity_id,
                        {"temperature": temperature},
                    )
                    await asyncio.sleep(1.5)
                    state = self.hass.states.get(entity_id)
                    current = state.attributes.get("temperature") if state else None
                    if current is not None and abs(float(current) - temperature) < 0.01:
                        break

    async def _call(self, domain: str, service: str, entity_id: str, extra=None) -> None:
        data = {"entity_id": entity_id}
        if extra:
            data.update(extra)
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # noqa: BLE001 - never break the mqtt thread
            _LOGGER.warning("Bemfa action %s.%s failed: %s", domain, service, err)
