"""Data update coordinator for the SensiHydro integration."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SensiHydroApiError,
    SensiHydroAuthError,
    SensiHydroClient,
    SensiHydroConnectionError,
)
from .const import DEFAULT_NOMINAL_VOLTAGE, DEFAULT_SCAN_INTERVAL, DOMAIN
from .local_api import LocalApiClient, LocalApiError
from . import hostmode, outdoor

_LOGGER = logging.getLogger(__name__)

type SensiHydroConfigEntry = ConfigEntry[SensiHydroCoordinator]

def _local_unit_parameter_values(local: dict[str, Any]) -> dict[str, str]:
    """Map a locally read indoor unit state to the cloud parameter codes."""
    firmware = local.get("firmware") or {}
    values: dict[str, str] = {}
    if firmware:
        values["val200"] = str(firmware.get("indoor_board", ""))
        values["val201"] = str(firmware.get("display_panel", ""))
        values["val202"] = str(firmware.get("code_high", ""))
        values["val203"] = str(firmware.get("code_middle", ""))
        values["val204"] = str(firmware.get("code_low", ""))
    if local.get("return_air_temperature") is not None:
        values["val205"] = f"{float(local['return_air_temperature']):.1f}℃"
    values["val206"] = "闭合" if local.get("float_switch") else "断开"
    values["val207"] = "开" if local.get("floor_valve") else "关"
    values["val208"] = "开" if local.get("drain_pump") else "关"
    values["val209"] = "开" if local.get("valve_direction") else "关"
    if local.get("fan_rpm") is not None:
        values["val210"] = f"{int(local['fan_rpm'])}转/分"
    if local.get("room_temperature") is not None:
        values["val213"] = f"{float(local['room_temperature']):.1f}℃"
    return values


# The slow-changing data (run-time parameter table, schedules, notices, ...)
# is refreshed at most every 5 minutes instead of on every poll.
EXTRA_UPDATE_INTERVAL = 300


class SensiHydroCoordinator(DataUpdateCoordinator[dict[int, dict[str, Any]]]):
    """Poll the cloud and keep the state of every indoor unit."""

    config_entry: SensiHydroConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: SensiHydroConfigEntry,
        client: SensiHydroClient,
        device_id: int,
        device_serialno: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        device_name: str = "SensiHydro",
        nominal_voltage: float = DEFAULT_NOMINAL_VOLTAGE,
        local_api_url: str = "",
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(15, scan_interval)),
        )
        self.client = client
        self.device_id = device_id
        self.device_serialno = device_serialno
        self.device_name = device_name
        self.nominal_voltage = nominal_voltage
        self.gateway_online: bool | None = None

        # Local LAN bridge (docker container). When reachable it provides
        # real-time state and direct control through the DTU's Modbus/TCP
        # tunnel; the cloud stays the fallback path.
        self.local_api_url = local_api_url
        self._local: LocalApiClient | None = None
        if local_api_url:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            self._local = LocalApiClient(
                async_get_clientsession(hass), local_api_url
            )
        self.local_available: bool = False
        self.local_status: dict[str, Any] = {}
        self.local_outdoor: dict[str, str] = {}
        self.host_mode: str | None = None
        self.host_mode_detail: dict[str, Any] = {}
        self.local_units: dict[str, dict[str, Any]] = {}

        # extra (slow) data, refreshed every EXTRA_UPDATE_INTERVAL seconds
        self.device: dict[str, Any] = {}
        self.device_params: dict[str, dict[str, Any]] = {}
        self.unit_params: dict[int, dict[str, dict[str, Any]]] = {}
        self.schedules: list[dict[str, Any]] = []
        self.bind_users: list[dict[str, Any]] = []
        self.notices: dict[str, Any] = {}
        self.electric: Any = None
        self._extra_updated: float = 0.0

    async def _async_update_data(self) -> dict[int, dict[str, Any]]:
        """Fetch all indoor units (and periodically the extra cloud data)."""
        try:
            units = await self.client.async_get_units(self.device_id)
            if not units:
                raise UpdateFailed("cloud returned an empty list of indoor units")

            # The cloud keeps serving cached unit data even when the DTU gateway
            # is offline; expose that separately so users know commands won't arrive.
            if self.device_serialno:
                self.gateway_online = await self.client.async_check_online(
                    self.device_serialno
                )
            else:
                self.gateway_online = None
        except SensiHydroAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SensiHydroConnectionError as err:
            raise UpdateFailed(f"cannot reach SensiHydro cloud: {err}") from err
        except SensiHydroApiError as err:
            raise UpdateFailed(f"SensiHydro cloud error: {err}") from err
        except Exception as err:  # noqa: BLE001 - keep the coordinator alive
            raise UpdateFailed(f"unexpected error: {err}") from err

        await self._async_update_local(units)

        data: dict[int, dict[str, Any]] = {}
        for unit in units:
            try:
                unit_id = int(unit["id"])
            except (KeyError, TypeError, ValueError):
                continue
            data[unit_id] = unit
        _LOGGER.debug("SensiHydro: polling %s units", len(data))

        now = time.monotonic()
        if now - self._extra_updated >= EXTRA_UPDATE_INTERVAL:
            await self._async_update_extra(data)
            self._extra_updated = now

        # Overlay the run-time parameter tables with live values read straight
        # from the controller (the cloud table is only a cached snapshot).
        await self._async_apply_local_parameters(data)

        return data

    async def _async_apply_local_parameters(
        self, units: dict[int, dict[str, Any]]
    ) -> None:
        """Patch the parameter tables with values read from the controller."""
        if not self.local_available or self._local is None:
            return

        # --- outdoor unit (val100..val124) -------------------------------
        if self.device_params:
            try:
                values = await outdoor.async_read_outdoor(self._local)
            except LocalApiError as err:
                _LOGGER.debug("SensiHydro: local outdoor parameters unavailable: %s", err)
            else:
                if values:
                    outdoor.apply(self.device_params, values)
                    self.local_outdoor = values

        # --- indoor units (val200..val213) -------------------------------
        if not self.local_units:
            return
        for unit_id, unit in units.items():
            local = self.local_units.get(str(unit.get("name") or ""))
            params = self.unit_params.get(unit_id)
            if not local or not params:
                continue
            values = _local_unit_parameter_values(local)
            for code, value in values.items():
                param = params.get(code)
                if isinstance(param, dict):
                    param["value"] = value

    async def _async_update_local(self, units: list[dict[str, Any]]) -> None:
        """Read the LAN bridge and overlay the live state onto the cloud data."""
        if self._local is None:
            return
        try:
            local_units = await self._local.async_get_state()
        except LocalApiError as err:
            if self.local_available:
                _LOGGER.info("SensiHydro local bridge unavailable: %s", err)
            self.local_available = False
            return
        self.local_available = True
        self.local_units = local_units
        try:
            self.host_mode_detail = await hostmode.async_read_host_mode(self._local)
            self.host_mode = self.host_mode_detail.get("mode")
        except LocalApiError as err:
            _LOGGER.debug("SensiHydro: host mode unavailable: %s", err)
        try:
            self.local_status = await self._local.async_status()
        except LocalApiError:
            self.local_status = {}

        for unit in units:
            name = str(unit.get("name") or "")
            local = local_units.get(name)
            if not local:
                continue
            unit["status"] = 1 if local.get("power") else 0
            unit["mode"] = local.get("mode")
            unit["targetTemperature"] = local.get("target_temperature")
            # The cloud's "actualTemperature" is the wired-controller room
            # temperature; use the local sensor for an up-to-date value.
            unit["actualTemperature"] = local.get("room_temperature")
            unit["windModel"] = 1 if local.get("fan_auto") else 0
            unit["windSpeed"] = 0 if local.get("fan_auto") else local.get("fan_speed")
            unit["local"] = {
                "return_air_temperature": local.get("return_air_temperature"),
                "room_temperature": local.get("room_temperature"),
                "floor_valve": local.get("floor_valve"),
                "fan_rpm": local.get("fan_rpm"),
            }

    async def _async_update_extra(self, units: dict[int, dict[str, Any]]) -> None:
        """Fetch the slow-changing details; a failure here is not fatal."""
        unit_id_by_name = {
            str(unit.get("name")): unit_id for unit_id, unit in units.items()
        }

        try:
            device = await self.client.async_get_device(self.device_id)
        except SensiHydroAuthError:
            raise
        except (SensiHydroApiError, SensiHydroConnectionError) as err:
            _LOGGER.debug("SensiHydro: device details unavailable: %s", err)
        else:
            if device:
                self.device = device

        try:
            service = await self.client.async_get_service_config(
                self.device_serialno
            )
        except SensiHydroAuthError:
            raise
        except (SensiHydroApiError, SensiHydroConnectionError) as err:
            _LOGGER.debug("SensiHydro: run-time parameters unavailable: %s", err)
        else:
            if service:
                self._parse_service_config(service, unit_id_by_name)

        try:
            self.schedules = await self.client.async_get_schedules(self.device_id)
        except SensiHydroAuthError:
            raise
        except (SensiHydroApiError, SensiHydroConnectionError) as err:
            _LOGGER.debug("SensiHydro: schedules unavailable: %s", err)

        try:
            self.bind_users = await self.client.async_get_bind_users(self.device_id)
        except SensiHydroAuthError:
            raise
        except (SensiHydroApiError, SensiHydroConnectionError) as err:
            _LOGGER.debug("SensiHydro: bound users unavailable: %s", err)

        try:
            self.notices = await self.client.async_get_notices()
        except SensiHydroAuthError:
            raise
        except (SensiHydroApiError, SensiHydroConnectionError) as err:
            _LOGGER.debug("SensiHydro: notices unavailable: %s", err)

        try:
            self.electric = await self.client.async_get_electric()
        except SensiHydroAuthError:
            raise
        except (SensiHydroApiError, SensiHydroConnectionError) as err:
            _LOGGER.debug("SensiHydro: electricity statistics unavailable: %s", err)

    def _parse_service_config(
        self, service: dict[str, Any], unit_id_by_name: dict[str, int]
    ) -> None:
        """Parse the run-time parameter tables into code -> parameter maps."""
        device_params: dict[str, dict[str, Any]] = {}
        for entry in service.get("paramDeviceDtoList") or []:
            for param in entry.get("deviceParamValueList") or []:
                code = param.get("code")
                if code:
                    device_params[str(code)] = param
            # Some firmwares put the per-unit parameters in the same entry.
        if device_params:
            self.device_params = device_params

        unit_params: dict[int, dict[str, dict[str, Any]]] = {}
        for entry in service.get("paramUnitDtoList") or []:
            unit_id = unit_id_by_name.get(str(entry.get("name")))
            if unit_id is None:
                continue
            params: dict[str, dict[str, Any]] = {}
            for key in ("unitParamValueList", "deviceParamValueList"):
                for param in entry.get(key) or []:
                    code = param.get("code")
                    if code:
                        params[str(code)] = param
            if params:
                unit_params[unit_id] = params
        if unit_params:
            self.unit_params = unit_params

        _LOGGER.debug(
            "SensiHydro: parsed %s device parameters and %s unit parameter sets",
            len(device_params),
            len(unit_params),
        )
