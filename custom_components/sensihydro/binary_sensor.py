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
    entities.append(
        SensiHydroFaultSensor(coordinator, "device", None, gateway_info)
    )
    for unit_id, unit in coordinator.data.items():
        unit_info = DeviceInfo(
            identifiers={(DOMAIN, str(unit_id))},
            name=unit.get("name") or f"Unit {unit_id}",
            manufacturer=MANUFACTURER,
            model=MODEL,
            via_device=(DOMAIN, str(coordinator.device_id)),
        )
        entities.append(
            SensiHydroFaultSensor(coordinator, "unit", unit_id, unit_info)
        )

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


class SensiHydroFaultSensor(
    CoordinatorEntity[SensiHydroCoordinator], BinarySensorEntity
):
    """Aggregated protection/fault state of the outdoor or an indoor unit."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "故障/保护"

    def __init__(
        self,
        coordinator: SensiHydroCoordinator,
        scope: str,
        unit_id: int | None,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the fault sensor."""
        super().__init__(coordinator)
        self._scope = scope
        self._unit_id = unit_id
        self._attr_device_info = device_info
        suffix = (
            f"{coordinator.device_id}_fault"
            if scope == "device"
            else f"{coordinator.device_id}_{unit_id}_fault"
        )
        self._attr_unique_id = f"{DOMAIN}_{suffix}"

    def _flags(self) -> dict[str, dict[str, Any]]:
        """Return the flag parameters of this scope."""
        if self._scope == "device":
            params = self.coordinator.device_params
        else:
            params = self.coordinator.unit_params.get(self._unit_id or -1) or {}
        return {
            str(code): param
            for code, param in params.items()
            if is_flag_code(str(code))
        }

    @property
    def is_on(self) -> bool | None:
        """Return True when any protection/fault flag is active."""
        flags = self._flags()
        if not flags:
            return None
        known = [
            parse_param_value(param.get("value")) for param in flags.values()
        ]
        known = [value for value in known if value is not None]
        if not known:
            return None
        return any(is_truthy(value) for value in known)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the individual flags."""
        flags = self._flags()
        active: list[str] = []
        raw: dict[str, Any] = {}
        for param in flags.values():
            value = parse_param_value(param.get("value"))
            if value is None:
                continue
            raw[str(param.get("name") or param.get("code"))] = value
            if is_truthy(value):
                active.append(str(param.get("name") or param.get("code")))
        return {
            "active_faults": active,
            "active_count": len(active),
            "total_flags": len(flags),
            "flags": raw,
        }
