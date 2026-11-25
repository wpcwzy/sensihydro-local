"""Host (outdoor unit) mode selector for the SensiHydro integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import hostmode
from .api import SensiHydroError
from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import SensiHydroConfigEntry, SensiHydroCoordinator
from .local_api import LocalApiError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SensiHydroConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the host mode selector."""
    coordinator = entry.runtime_data
    async_add_entities([SensiHydroHostMode(coordinator)])


class SensiHydroHostMode(
    CoordinatorEntity[SensiHydroCoordinator], SelectEntity
):
    """Outdoor-unit mode: 常规 / 节能 / 静音 / 强热 (+ combinations)."""

    _attr_has_entity_name = True
    _attr_name = "主机模式"
    _attr_options = hostmode.MODE_ORDER
    _attr_icon = "mdi:heat-pump"

    def __init__(self, coordinator: SensiHydroCoordinator) -> None:
        """Initialize the selector."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.device_id}_host_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.device_id))},
            name=coordinator.device_name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def current_option(self) -> str | None:
        """Return the current host mode."""
        return self.coordinator.host_mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the individual flags."""
        detail = self.coordinator.host_mode_detail or {}
        return {
            "eco": detail.get("eco"),
            "quiet": detail.get("quiet"),
            "boost": detail.get("boost"),
            "water_setpoint": detail.get("water_setpoint"),
            "local": self.coordinator.local_available,
        }

    async def async_select_option(self, option: str) -> None:
        """Apply a host mode locally, falling back to the cloud."""
        coordinator = self.coordinator
        client = getattr(coordinator, "_local", None)
        try:
            if coordinator.local_available and client is not None:
                await hostmode.async_set_host_mode(client, option)
            else:
                await coordinator.client.async_set_host_mode(
                    coordinator.device_id, option
                )
        except (LocalApiError, SensiHydroError) as err:
            raise HomeAssistantError(
                f"{self.name}: 设置主机模式失败（{err}）"
            ) from err
        await coordinator.async_request_refresh()
