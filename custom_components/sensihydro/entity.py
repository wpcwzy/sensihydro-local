"""Base entity for the SensiHydro integration."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import SensiHydroCoordinator


class SensiHydroEntity(CoordinatorEntity[SensiHydroCoordinator]):
    """Common behaviour of all SensiHydro entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SensiHydroCoordinator, unit_id: int) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._unit_id = unit_id
        unit = coordinator.data.get(unit_id, {})
        name = unit.get("name") or f"Unit {unit_id}"
        # Do not set ``suggested_area``: Home Assistant auto-creates an area
        # with this name, and the area is prefixed to the entity_id, which
        # would turn ``climate.ke_ting`` into ``climate.ke_ting_ke_ting``.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(unit_id))},
            name=name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            via_device=(DOMAIN, str(coordinator.device_id)),
        )

    @property
    def unit(self) -> dict[str, Any]:
        """Return the last known cloud state of this indoor unit."""
        return self.coordinator.data.get(self._unit_id, {})

    @property
    def available(self) -> bool:
        """Return if the unit data is available."""
        return super().available and bool(self.unit)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose raw cloud fields for debugging."""
        unit = self.unit
        return {
            "unit_id": self._unit_id,
            "sequence": unit.get("sequence"),
            "cloud_mode": unit.get("mode"),
            "wind_speed": unit.get("windSpeed"),
            "wind_model": unit.get("windModel"),
            "sleep": unit.get("sleep"),
            "min_gear": unit.get("minGear"),
            "alternating_flag": unit.get("alternatingFlag"),
            "device_id": self.coordinator.device_id,
            "token_expires": self.coordinator.client.token_expires,
        }
