"""Binary sensors for the SensiHydro integration.

The DTU gateway is a pure cloud TCP client. The cloud keeps serving cached
unit data even when the gateway is offline, so this connectivity entity makes
it obvious when commands can no longer reach the physical air conditioner.

In addition, the cloud run-time parameter table contains protection/fault
flags for the outdoor unit and for every indoor unit; they are aggregated
into one ``故障/保护`` binary sensor per device (with the individual flags in
its attributes) instead of flooding Home Assistant with 130 entities.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import SensiHydroConfigEntry, SensiHydroCoordinator
from .util import is_flag_code, is_truthy, parse_param_value


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SensiHydroConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the cloud connectivity and fault sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [SensiHydroCloudLink(coordinator)]
    if coordinator.local_api_url:
        entities.append(SensiHydroLocalBridge(coordinator))

    gateway_info = DeviceInfo(
        identifiers={(DOMAIN, str(coordinator.device_id))},
        name=coordinator.device_name,
        manufacturer=MANUFACTURER,
        model=MODEL,
    )
    # Protection/fault flags are aggregated in the "故障汇总" sensor of the
    # sensor platform, so no per-unit fault binary sensors are created here.

    async_add_entities(entities)


class SensiHydroCloudLink(CoordinatorEntity[SensiHydroCoordinator], BinarySensorEntity):
    """Whether the air conditioner gateway is connected to the cloud."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "cloud_link"
    _attr_name = "云端链路"

    def __init__(self, coordinator: SensiHydroCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.device_id}_cloud_link"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.device_id))},
            name=coordinator.device_name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def is_on(self) -> bool | None:
        """Return True when the gateway is online."""
        return self.coordinator.gateway_online


class SensiHydroLocalBridge(
    CoordinatorEntity[SensiHydroCoordinator], BinarySensorEntity
):
    """Whether the local LAN bridge is reachable and controlling the units."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "本地桥接"

    def __init__(self, coordinator: SensiHydroCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{DOMAIN}_{coordinator.device_id}_local_bridge"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.device_id))},
            name=coordinator.device_name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def is_on(self) -> bool:
        """Return True when the LAN bridge answers."""
        return self.coordinator.local_available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the bridge URL and relay health."""
        status = self.coordinator.local_status or {}
        return {
            "url": self.coordinator.local_api_url,
            "dtu_connected": status.get("dtu_connected"),
            "cloud_connected": status.get("cloud_connected"),
            "injected_requests": status.get("injected_requests"),
        }
