"""Climate platform for the SensiHydro (艾默生 心海悦) integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import SensiHydroError, SensiHydroWriteError
from .local_api import LocalApiError
from .const import (
    DOMAIN,
    FAN_AUTO,
    FAN_MODES,
    HVAC_TO_MODE,
    MAX_TEMP,
    MIN_TEMP,
    MODE_FAN,
    MODE_FLOOR,
    MODE_HEAT,
    MODE_HEAT_FLOOR,
    MODE_NAMES,
    MODE_TO_HVAC,
    PRESET_FLOOR,
    TEMP_STEP,
)
from .coordinator import SensiHydroConfigEntry, SensiHydroCoordinator
from .entity import SensiHydroEntity

_LOGGER = logging.getLogger(__name__)

PRESET_NONE = "无"

HVAC_MODES = [
    HVACMode.OFF,
    HVACMode.COOL,
    HVACMode.HEAT,
    HVACMode.HEAT_COOL,
    HVACMode.FAN_ONLY,
    HVACMode.DRY,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SensiHydroConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the climate entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        SensiHydroClimate(coordinator, unit_id)
        for unit_id in coordinator.data
    )


def _int(value: Any, default: int = 0) -> int:
    """Best effort int."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    """Best effort float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SensiHydroClimate(SensiHydroEntity, ClimateEntity):
    """A Sensi Hydro indoor unit exposed as a Home Assistant thermostat."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = TEMP_STEP
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_hvac_modes = HVAC_MODES
    _attr_fan_modes = FAN_MODES
    _attr_preset_modes = [PRESET_NONE, PRESET_FLOOR]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: SensiHydroCoordinator, unit_id: int) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator, unit_id)
        self._attr_unique_id = f"{DOMAIN}_{unit_id}"
        self._attr_translation_key = None

    # ------------------------------------------------------------- read side

    @property
    def is_on(self) -> bool:
        """Return if the unit is powered on."""
        return _int(self.unit.get("status")) == 1

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        if not self.is_on:
            return HVACMode.OFF
        return MODE_TO_HVAC.get(_int(self.unit.get("mode")), HVACMode.HEAT)

    @property
    def current_temperature(self) -> float | None:
        """Return the measured room temperature."""
        value = self.unit.get("actualTemperature")
        return _float(value) if value is not None else None

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        value = self.unit.get("targetTemperature")
        return _float(value) if value is not None else None

    @property
    def fan_mode(self) -> str:
        """Return the fan mode."""
        if _int(self.unit.get("windModel")) == 1:
            return FAN_AUTO
        return str(max(1, min(10, _int(self.unit.get("windSpeed"), 1))))

    @property
    def preset_mode(self) -> str:
        """Return the preset mode."""
        if _int(self.unit.get("mode")) == MODE_FLOOR:
            return PRESET_FLOOR
        return PRESET_NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw device mode as well."""
        attrs = super().extra_state_attributes
        mode = _int(self.unit.get("mode"))
        attrs["mode_name"] = MODE_NAMES.get(mode, str(mode))
        return attrs

    # ------------------------------------------------------------ write side

    @property
    def _unit_name(self) -> str:
        """Return the unit name used by the local bridge."""
        return str(self.unit.get("name") or "")

    async def _async_local(self, call) -> bool:
        """Try a write through the LAN bridge.

        Returns True when the write was handled locally; False means the
        caller should fall back to the vendor cloud.
        """
        coordinator = self.coordinator
        client = getattr(coordinator, "_local", None)
        if not coordinator.local_available or client is None:
            return False
        try:
            await call(client)
        except LocalApiError as err:
            raise HomeAssistantError(f"{self.name}: 本地写入失败（{err}）") from err
        await coordinator.async_request_refresh()
        return True

    async def _async_write(self, coro: Any) -> None:
        """Run one write against the cloud and refresh the state."""
        try:
            await coro
        except SensiHydroWriteError as err:
            raise HomeAssistantError(
                f"{self.name}: 云端写入未生效（{err}）"
            ) from err
        except SensiHydroError as err:
            raise HomeAssistantError(f"{self.name}: 云端写入失败（{err}）") from err
        await self.coordinator.async_refresh()

    def _client_kwargs(self) -> tuple[int, str, dict[str, Any]]:
        """Return the arguments needed by every cloud write call."""
        return (
            self.coordinator.device_id,
            self.coordinator.device_serialno,
            self.unit,
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            raise HomeAssistantError("未提供目标温度")
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        handled = await self._async_local(
            lambda client: client.async_set_target(self._unit_name, float(temperature))
        )
        if not handled:
            await self._async_write(
                self.coordinator.client.async_set_temperature(
                    *self._client_kwargs(), float(temperature)
                )
            )
        if hvac_mode is not None:
            await self.async_set_hvac_mode(hvac_mode)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode."""
        device_id, serialno, unit = self._client_kwargs()
        if hvac_mode == HVACMode.OFF:
            handled = await self._async_local(
                lambda client: client.async_set_power(self._unit_name, False)
            )
            if handled:
                return
            await self._async_write(
                self.coordinator.client.async_set_power(
                    device_id, serialno, unit, False
                )
            )
            return

        mode = HVAC_TO_MODE.get(hvac_mode)
        if mode is None:
            raise HomeAssistantError(f"不支持的模式: {hvac_mode}")

        async def _local_sequence(client):
            if not self.is_on:
                await client.async_set_power(self._unit_name, True)
            await client.async_set_mode(self._unit_name, mode)

        if await self._async_local(_local_sequence):
            return

        if not self.is_on:
            await self._async_write(
                self.coordinator.client.async_set_power(
                    device_id, serialno, unit, True
                )
            )
            unit = self.unit
        await self._async_write(
            self.coordinator.client.async_set_mode(device_id, serialno, unit, mode)
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan mode."""
        if _int(self.unit.get("mode")) == MODE_FAN:
            raise HomeAssistantError("送风模式下不能调节风速")

        def _fan(api) -> Any:
            if fan_mode == FAN_AUTO:
                return api.async_set_fan(self._unit_name, auto=True)
            return api.async_set_fan(self._unit_name, speed=int(fan_mode), auto=False)

        if await self._async_local(_fan):
            return
        await self._async_write(
            self.coordinator.client.async_set_fan_mode(
                *self._client_kwargs(), fan_mode
            )
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the preset mode (electric floor heating)."""
        device_id, serialno, unit = self._client_kwargs()
        if preset_mode == PRESET_FLOOR:
            handled = await self._async_local(
                lambda client: client.async_set_mode(self._unit_name, MODE_FLOOR)
            )
            if handled:
                return
            await self._async_write(
                self.coordinator.client.async_set_mode(
                    device_id, serialno, unit, MODE_FLOOR
                )
            )
            return
        if _int(unit.get("mode")) == MODE_FLOOR:
            handled = await self._async_local(
                lambda client: client.async_set_mode(self._unit_name, MODE_HEAT)
            )
            if handled:
                return
            await self._async_write(
                self.coordinator.client.async_set_mode(
                    device_id, serialno, unit, MODE_HEAT
                )
            )

    async def async_turn_on(self) -> None:
        """Turn the unit on."""
        handled = await self._async_local(
            lambda client: client.async_set_power(self._unit_name, True)
        )
        if handled:
            return
        await self._async_write(
            self.coordinator.client.async_set_power(
                *self._client_kwargs(), True
            )
        )

    async def async_turn_off(self) -> None:
        """Turn the unit off."""
        handled = await self._async_local(
            lambda client: client.async_set_power(self._unit_name, False)
        )
        if handled:
            return
        await self._async_write(
            self.coordinator.client.async_set_power(
                *self._client_kwargs(), False
            )
        )
