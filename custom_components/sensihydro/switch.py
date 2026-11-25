"""Switch platform (sleep mode) for the SensiHydro integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import SensiHydroError, SensiHydroWriteError
from .const import DOMAIN
from .coordinator import SensiHydroConfigEntry, SensiHydroCoordinator
from .entity import SensiHydroEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SensiHydroConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sleep switches."""
    coordinator = entry.runtime_data
    async_add_entities(
        SensiHydroSleepSwitch(coordinator, unit_id) for unit_id in coordinator.data
    )


class SensiHydroSleepSwitch(SensiHydroEntity, SwitchEntity):
    """Sleep mode of a Sensi Hydro indoor unit."""

    _attr_translation_key = "sleep"
    _attr_icon = "mdi:sleep"

    def __init__(self, coordinator: SensiHydroCoordinator, unit_id: int) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, unit_id)
        self._attr_unique_id = f"{DOMAIN}_{unit_id}_sleep"

    @property
    def is_on(self) -> bool:
        """Return if sleep mode is enabled."""
        try:
            return int(float(self.unit.get("sleep"))) == 1
        except (TypeError, ValueError):
            return False

    async def _async_write(self, coro: Any) -> None:
        """Run the write and refresh the coordinator."""
        try:
            await coro
        except SensiHydroWriteError as err:
            raise HomeAssistantError(
                f"{self.name}: 云端写入未生效（{err}）"
            ) from err
        except SensiHydroError as err:
            raise HomeAssistantError(f"{self.name}: 云端写入失败（{err}）") from err
        await self.coordinator.async_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable sleep mode."""
        await self._async_write(
            self.coordinator.client.async_set_sleep(
                self.coordinator.device_id,
                self.coordinator.device_serialno,
                self.unit,
                True,
            )
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable sleep mode."""
        await self._async_write(
            self.coordinator.client.async_set_sleep(
                self.coordinator.device_id,
                self.coordinator.device_serialno,
                self.unit,
                False,
            )
        )
