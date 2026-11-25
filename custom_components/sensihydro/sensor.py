"""Diagnostic sensors for the SensiHydro integration.

These entities expose the additional data the cloud offers but that is not
part of the thermostat itself, for example:

* outdoor-unit run-time parameters (water/air temperatures, pressures,
  compressor speed, AC voltage/current, valve openings, program versions,
  protection/fault flags) - ``customservice/selectConfigById``;
* per indoor-unit parameters (software versions, return-air temperature,
  float switch, floor-heating / drain-pump valve states, motor speed,
  fault codes) - ``customservice/selectConfigById``;
* device metadata (last cloud update, bound accounts, timer schedules,
  notice counts, electricity statistics).

None of these entities are exposed to HomeKit: the HomeKit bridge created
for this integration only includes the 6 climate + 6 sleep switch entities.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MIN_VALID_VOLTAGE, MODEL
from .coordinator import SensiHydroConfigEntry, SensiHydroCoordinator
from .util import is_flag_code, is_truthy, parse_param, parse_param_value

_LOGGER = logging.getLogger(__name__)

# Fields of unit/list that are worth exposing as separate sensors.
UNIT_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("mode", "云端模式原值", True),
    ("windSpeed", "风速档位", True),
    ("windModel", "风速模式", True),
    ("minGear", "最小档位", False),
    ("alternatingFlag", "交流/直流类型", False),
    ("alternatingMaxGear", "交流最大档位", False),
)

WIND_MODEL_NAMES = {0: "手动", 1: "自动"}
ALTERNATING_NAMES = {0: "直流", 1: "交流"}


def _param_meta(name: str, code: str) -> dict[str, Any]:
    """Derive unit / device class / statistics settings from a parameter name.

    The cloud returns values with a unit suffix (``9.9℃``, ``21.6Bar``,
    ``89HZ``, ``0转/分``); mapping them properly turns the diagnostic
    sensors into numeric, statistic-enabled entities so the Home Assistant
    history graphs show real line charts instead of discrete text states.
    """
    if is_flag_code(code):
        return {"flag": True}
    if "温度" in name:
        return {
            "unit": "°C",
            "device_class": SensorDeviceClass.TEMPERATURE,
            "state_class": SensorStateClass.MEASUREMENT,
            "precision": 1,
        }
    if "电压" in name:
        return {
            "unit": "V",
            "device_class": SensorDeviceClass.VOLTAGE,
            "state_class": SensorStateClass.MEASUREMENT,
            "precision": 1,
        }
    if "电流" in name:
        return {
            "unit": "A",
            "device_class": SensorDeviceClass.CURRENT,
            "state_class": SensorStateClass.MEASUREMENT,
            "precision": 1,
        }
    if "压力" in name:
        return {
            "unit": "bar",
            "device_class": SensorDeviceClass.PRESSURE,
            "state_class": SensorStateClass.MEASUREMENT,
            "precision": 1,
        }
    if "转速" in name:
        if "压缩机" in name:
            return {
                "unit": "Hz",
                "device_class": SensorDeviceClass.FREQUENCY,
                "state_class": SensorStateClass.MEASUREMENT,
                "precision": 0,
            }
        return {
            "unit": "r/min",
            "state_class": SensorStateClass.MEASUREMENT,
            "precision": 0,
        }
    if "开度" in name:
        return {"state_class": SensorStateClass.MEASUREMENT, "precision": 0}
    if "版本" in name or "识别码" in name:
        return {"precision": 0}
    return {}


def _gateway_device_info(coordinator: SensiHydroCoordinator) -> DeviceInfo:
    """Device info of the outdoor unit / gateway."""
    return DeviceInfo(
        identifiers={(DOMAIN, str(coordinator.device_id))},
        name=coordinator.device_name,
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


def _unit_device_info(
    coordinator: SensiHydroCoordinator, unit_id: int
) -> DeviceInfo:
    """Device info of one indoor unit."""
    unit = coordinator.data.get(unit_id, {})
    return DeviceInfo(
        identifiers={(DOMAIN, str(unit_id))},
        name=unit.get("name") or f"Unit {unit_id}",
        manufacturer=MANUFACTURER,
        model=MODEL,
        via_device=(DOMAIN, str(coordinator.device_id)),
    )


def _num(value: Any) -> float | int | None:
    """Return a numeric value if possible."""
    parsed = parse_param_value(value)
    if isinstance(parsed, (int, float)):
        return parsed
    return None


def _timestamp_ms(value: Any) -> datetime | None:
    """Convert a cloud millisecond timestamp to a datetime."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def power_data(coordinator: SensiHydroCoordinator) -> dict[str, Any]:
    """Calculate the estimated AC input power from U and I.

    The cloud exposes ``AC输入电压(RMS)`` (val113) and ``AC输入电流（RMS）``
    (val114). Some firmwares report ``0V`` for the voltage even while the
    unit is running; in that case fall back to the configured nominal
    voltage (default 220 V, configurable in the integration options).
    The calculation assumes a power factor of ~1, i.e. it is an estimate.
    """
    voltage_param = coordinator.device_params.get("val113") or {}
    current_param = coordinator.device_params.get("val114") or {}
    voltage_parsed = parse_param(voltage_param.get("value")).value
    current_parsed = parse_param(current_param.get("value")).value

    measured_voltage = (
        float(voltage_parsed)
        if isinstance(voltage_parsed, (int, float))
        else None
    )
    current = (
        float(current_parsed) if isinstance(current_parsed, (int, float)) else None
    )
    nominal = float(coordinator.nominal_voltage or 220.0)

    if measured_voltage is not None and measured_voltage >= MIN_VALID_VOLTAGE:
        voltage = measured_voltage
        source = "measured"
    else:
        voltage = nominal
        source = "nominal"

    power = None if current is None else round(voltage * current, 1)
    return {
        "power": power,
        "voltage": voltage,
        "voltage_source": source,
        "voltage_measured": measured_voltage,
        "current": current,
        "nominal_voltage": nominal,
        "power_factor_assumed": 1.0,
        "formula": "P = U x I",
    }


def _coerce_restored_state(state: str) -> float | int | None:
    """Convert a restored numeric state back to a number."""
    try:
        return int(state)
    except ValueError:
        try:
            return float(state)
        except ValueError:
            return None


class SensiHydroSensor(
    CoordinatorEntity[SensiHydroCoordinator], SensorEntity, RestoreEntity
):
    """Generic diagnostic sensor fed by a getter callback.

    Diagnostic values come from a cloud table that occasionally returns only a
    subset of its parameters.  The sensor therefore remembers the last value it
    saw (and restores it after a restart) instead of flipping to ``unknown``,
    which used to break the history graph into fragments.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: SensiHydroCoordinator,
        *,
        unique_id_suffix: str,
        name: str,
        device_info: DeviceInfo,
        getter: Callable[[SensiHydroCoordinator], Any],
        attrs_getter: Callable[[SensiHydroCoordinator], dict[str, Any]] | None = None,
        native_unit: str | None = None,
        device_class: SensorDeviceClass | None = None,
        state_class: SensorStateClass | None = None,
        enabled: bool = True,
        entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{unique_id_suffix}"
        self._attr_name = name
        self._attr_device_info = device_info
        self._attr_native_unit_of_measurement = native_unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_entity_registry_enabled_default = enabled
        self._attr_entity_category = entity_category
        self._getter = getter
        self._attrs_getter = attrs_getter
        self._last_value: Any = None
        self._restored_value: float | int | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known value so a restart does not create gaps."""
        await super().async_added_to_hass()
        if self.device_class == SensorDeviceClass.TIMESTAMP:
            return
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            return
        self._restored_value = _coerce_restored_state(last_state.state)

    @property
    def native_value(self) -> Any:
        """Return the current value, falling back to the last known one."""
        try:
            value = self._getter(self.coordinator)
        except (KeyError, TypeError, ValueError):
            value = None
        if value is not None:
            self._last_value = value
            return value
        if self._last_value is not None:
            return self._last_value
        return self._restored_value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes."""
        if self._attrs_getter is None:
            return None
        try:
            return self._attrs_getter(self.coordinator)
        except (KeyError, TypeError, ValueError):
            return None


class SensiHydroFaultSummarySensor(SensiHydroSensor):
    """All protection/fault flags aggregated into one readable sensor.

    One entity for the whole system: state is ``正常`` when every flag is
    normal, otherwise it lists the active fault names (comma separated,
    prefixed with the unit name for indoor-unit faults).
    """

    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: SensiHydroCoordinator) -> None:
        super().__init__(
            coordinator,
            unique_id_suffix=f"{coordinator.device_id}_fault_summary",
            name="故障汇总",
            device_info=_gateway_device_info(coordinator),
            getter=self._summary,
            attrs_getter=self._attrs,
        )

    def _active(self) -> tuple[int, list[str], dict[str, str]]:
        """Return (known flag count, active fault labels, raw flag values)."""
        known = 0
        labels: list[str] = []
        raw: dict[str, str] = {}
        for code, param in self.coordinator.device_params.items():
            if not is_flag_code(str(code)):
                continue
            value = parse_param_value(param.get("value"))
            if value is None:
                continue
            known += 1
            name = str(param.get("name") or code)
            raw[name] = str(value)
            if is_truthy(value):
                labels.append(name)
        for unit_id, params in self.coordinator.unit_params.items():
            unit_name = str(
                (self.coordinator.data.get(unit_id) or {}).get("name")
                or f"Unit {unit_id}"
            )
            for code, param in params.items():
                if not is_flag_code(str(code)):
                    continue
                value = parse_param_value(param.get("value"))
                if value is None:
                    continue
                known += 1
                name = str(param.get("name") or code)
                raw[f"{unit_name} {name}"] = str(value)
                if is_truthy(value):
                    labels.append(f"{unit_name} {name}")
        # de-duplicate while keeping order
        ordered: list[str] = []
        for label in labels:
            if label not in ordered:
                ordered.append(label)
        return known, ordered, raw

    def _summary(self, _coordinator: SensiHydroCoordinator) -> str | None:
        known, labels, _raw = self._active()
        if known == 0:
            return None  # no flag data at all -> keep the previous value
        return ", ".join(labels) if labels else "正常"

    def _attrs(self, _coordinator: SensiHydroCoordinator) -> dict[str, Any]:
        known, labels, raw = self._active()
        return {
            "active_count": len(labels),
            "active_faults": labels,
            "known_flags": known,
            "flags": raw,
        }


class SensiHydroParamSensor(SensiHydroSensor):
    """One parameter of the cloud run-time parameter table."""

    def __init__(
        self,
        coordinator: SensiHydroCoordinator,
        scope: str,
        code: str,
        param: dict[str, Any],
        unit_id: int | None = None,
    ) -> None:
        """Initialize the parameter sensor."""
        name = str(param.get("name") or code)
        meta = _param_meta(name, code)
        if scope == "unit" and unit_id is not None:
            unique = f"{coordinator.device_id}_{unit_id}_{code}"
            device_info = _unit_device_info(coordinator, unit_id)
        else:
            unique = f"{coordinator.device_id}_{code}"
            device_info = _gateway_device_info(coordinator)
        super().__init__(
            coordinator,
            unique_id_suffix=unique,
            name=name,
            device_info=device_info,
            getter=self._value,
            attrs_getter=self._attrs,
            native_unit=meta.get("unit"),
            device_class=meta.get("device_class"),
            state_class=meta.get("state_class"),
            enabled=True,
        )
        self._scope = scope
        self._code = code
        self._unit_id = unit_id
        self._is_flag = bool(meta.get("flag"))
        if "precision" in meta:
            self._attr_suggested_display_precision = meta["precision"]

    def _param(self) -> dict[str, Any]:
        """Return the raw parameter dict."""
        if self._scope == "unit" and self._unit_id is not None:
            return (self.coordinator.unit_params.get(self._unit_id) or {}).get(
                self._code, {}
            )
        return self.coordinator.device_params.get(self._code, {})

    def _value(self, coordinator: SensiHydroCoordinator) -> Any:
        """Return the parsed value (numeric when possible)."""
        raw = self._param().get("value")
        if self._is_flag:
            if parse_param_value(raw) is None:
                return None
            return 1 if is_truthy(raw) else 0
        return parse_param(raw).value

    def _attrs(self, coordinator: SensiHydroCoordinator) -> dict[str, Any]:
        """Return debugging details of the parameter."""
        param = self._param()
        raw = param.get("value")
        return {
            "code": param.get("code", self._code),
            "raw_value": raw,
            "parsed_unit": parse_param(raw).unit,
            "label": param.get("label"),
            "style": param.get("style"),
        }


class SensiHydroUnitFieldSensor(SensiHydroSensor):
    """One raw field of an indoor unit (fan gear, AC type, ...)."""

    def __init__(
        self,
        coordinator: SensiHydroCoordinator,
        unit_id: int,
        field: str,
        name: str,
        enabled: bool = True,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            unique_id_suffix=f"{coordinator.device_id}_{unit_id}_{field}",
            name=name,
            device_info=_unit_device_info(coordinator, unit_id),
            getter=self._value,
            attrs_getter=self._attrs,
            enabled=enabled,
        )
        self._unit_id = unit_id
        self._field = field

    def _raw(self) -> Any:
        """Return the raw field value."""
        return (self.coordinator.data.get(self._unit_id) or {}).get(self._field)

    def _value(self, coordinator: SensiHydroCoordinator) -> Any:
        """Return the parsed field value."""
        raw = self._raw()
        if self._field == "windModel":
            value = _num(raw)
            return WIND_MODEL_NAMES.get(int(value)) if value is not None else None
        if self._field == "alternatingFlag":
            value = _num(raw)
            return (
                ALTERNATING_NAMES.get(int(value)) if value is not None else None
            )
        return _num(raw)

    def _attrs(self, coordinator: SensiHydroCoordinator) -> dict[str, Any]:
        """Return raw value and field name."""
        return {"field": self._field, "raw_value": self._raw()}


class SensiHydroEnergySensor(
    CoordinatorEntity[SensiHydroCoordinator], SensorEntity, RestoreEntity
):
    """Cumulative energy (kWh) estimated by integrating the input power."""

    _attr_has_entity_name = True
    _attr_name = "累计用电量(估算)"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: SensiHydroCoordinator) -> None:
        """Initialize the energy sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.device_id}_energy"
        self._attr_device_info = _gateway_device_info(coordinator)
        self._total = 0.0
        self._last_time: float | None = None
        self._last_power: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the previous total."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            try:
                self._total = float(last_state.state)
            except (TypeError, ValueError):
                self._total = 0.0
        self._last_time = None
        self._last_power = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Integrate power over time (trapezoid)."""
        now = time.monotonic()
        power = power_data(self.coordinator)["power"]
        if (
            self._last_time is not None
            and self._last_power is not None
            and power is not None
        ):
            delta = now - self._last_time
            # Ignore long gaps (HA was sleeping / restarting)
            if 0 < delta <= 900:
                self._total += (
                    (self._last_power + power) / 2 * delta / 3_600_000
                )
                self._attr_native_value = round(self._total, 3)
        self._last_time = now
        if power is not None:
            self._last_power = power
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        """Return the accumulated energy in kWh."""
        return round(self._total, 3)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SensiHydroConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the diagnostic sensors."""
    coordinator = entry.runtime_data
    gateway = _gateway_device_info(coordinator)
    entities: list[SensiHydroSensor] = []

    # --- device / gateway level metadata -------------------------------
    entities.extend(
        [
            SensiHydroSensor(
                coordinator,
                unique_id_suffix=f"{coordinator.device_id}_cloud_last_update",
                name="云端最后上报",
                device_info=gateway,
                getter=lambda c: _timestamp_ms((c.device or {}).get("updateTime")),
                attrs_getter=lambda c: {
                    "raw_update_time": (c.device or {}).get("updateTime")
                },
                device_class=SensorDeviceClass.TIMESTAMP,
            ),
            SensiHydroSensor(
                coordinator,
                unique_id_suffix=f"{coordinator.device_id}_device_mode",
                name="设备模式(deviceMode)",
                device_info=gateway,
                getter=lambda c: (c.device or {}).get("deviceMode"),
            ),
            SensiHydroSensor(
                coordinator,
                unique_id_suffix=f"{coordinator.device_id}_bind_user_count",
                name="绑定用户数",
                device_info=gateway,
                getter=lambda c: len(c.bind_users),
                attrs_getter=lambda c: {
                    "users": [
                        {
                            "userName": user.get("userName"),
                            "mobilePhone": user.get("mobilePhone"),
                            "primaryFlag": user.get("primaryFlag"),
                        }
                        for user in c.bind_users
                    ]
                },
            ),
            SensiHydroSensor(
                coordinator,
                unique_id_suffix=f"{coordinator.device_id}_schedule_count",
                name="云端定时任务数",
                device_info=gateway,
                getter=lambda c: len(c.schedules),
                attrs_getter=lambda c: {"schedules": c.schedules},
            ),
            SensiHydroSensor(
                coordinator,
                unique_id_suffix=f"{coordinator.device_id}_failure_notices",
                name="故障通知数",
                device_info=gateway,
                getter=lambda c: _notice_count(c, "failure"),
                attrs_getter=lambda c: {"notices": c.notices.get("failure")},
            ),
            SensiHydroSensor(
                coordinator,
                unique_id_suffix=f"{coordinator.device_id}_maintenance_notices",
                name="维护通知数",
                device_info=gateway,
                getter=lambda c: _notice_count(c, "maintenance"),
                attrs_getter=lambda c: {"notices": c.notices.get("maintenance")},
            ),
            SensiHydroSensor(
                coordinator,
                unique_id_suffix=f"{coordinator.device_id}_input_power",
                name="输入功率(估算)",
                device_info=gateway,
                getter=lambda c: power_data(c)["power"],
                attrs_getter=power_data,
                native_unit="W",
                device_class=SensorDeviceClass.POWER,
                state_class=SensorStateClass.MEASUREMENT,
                entity_category=None,
            ),
            SensiHydroEnergySensor(coordinator),
            SensiHydroSensor(
                coordinator,
                unique_id_suffix=f"{coordinator.device_id}_electric",
                name="用电统计(原始)",
                device_info=gateway,
                getter=lambda c: None
                if c.electric is None
                else len(c.electric.get("result") or [])
                if isinstance(c.electric, dict)
                else None,
                attrs_getter=lambda c: {"raw": c.electric},
                enabled=False,
            ),
        ]
    )

    # Protection/fault flags are aggregated into a single readable sensor;
    # parameter (temperature/pressure/...) sensors stay independent.
    _remove_legacy_fault_entities(hass, entry, coordinator)
    entities.append(SensiHydroFaultSummarySensor(coordinator))

    # --- run-time parameter table of the outdoor unit ------------------
    for code, param in coordinator.device_params.items():
        if is_flag_code(str(code)):
            continue
        entities.append(SensiHydroParamSensor(coordinator, "device", code, param))

    # --- per indoor unit fields and parameters -------------------------
    for unit_id in coordinator.data:
        for field, name, enabled in UNIT_FIELDS:
            entities.append(
                SensiHydroUnitFieldSensor(
                    coordinator, unit_id, field, name, enabled=enabled
                )
            )
        for code, param in (coordinator.unit_params.get(unit_id) or {}).items():
            if is_flag_code(str(code)):
                continue
            entities.append(
                SensiHydroParamSensor(coordinator, "unit", code, param, unit_id)
            )

    _LOGGER.debug("SensiHydro: adding %s diagnostic sensors", len(entities))
    async_add_entities(entities)


def _remove_legacy_fault_entities(
    hass: HomeAssistant,
    entry: SensiHydroConfigEntry,
    coordinator: SensiHydroCoordinator,
) -> None:
    """Delete the per-flag sensors and fault binary sensors of older versions."""
    unique_ids: set[str] = {f"{DOMAIN}_{coordinator.device_id}_fault"}
    for unit_id in coordinator.data:
        unique_ids.add(f"{DOMAIN}_{coordinator.device_id}_{unit_id}_fault")
    for code in coordinator.device_params:
        if is_flag_code(str(code)):
            unique_ids.add(f"{DOMAIN}_{coordinator.device_id}_{code}")
    for unit_id, params in coordinator.unit_params.items():
        for code in params:
            if is_flag_code(str(code)):
                unique_ids.add(f"{DOMAIN}_{coordinator.device_id}_{unit_id}_{code}")
    registry = er.async_get(hass)
    removed = 0
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.unique_id in unique_ids:
            registry.async_remove(registry_entry.entity_id)
            removed += 1
    if removed:
        _LOGGER.info(
            "SensiHydro: removed %s legacy fault entities "
            "(all fault flags are now shown by the fault summary sensor)",
            removed,
        )


def _notice_count(coordinator: SensiHydroCoordinator, key: str) -> int | None:
    """Return the number of notices of one kind."""
    value = coordinator.notices.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        return len(value)
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return len(value)
    return 0
