"""Register map of the Sensi Hydro (心海悦) indoor units, verified on real hardware.

There are seven unit slots in the controller (one is unused).  Every unit owns

* a **state block** of 20 registers::

      +0   indoor board firmware version
      +1   display panel firmware version
      +2   identification code, high word
      +3   identification code, middle word
      +4   identification code, low word
      +5   return-air temperature      -> (raw - 3000) / 100  degC
      +6   float switch                (0/1)
      +7   floor-heating valve relay   (0/1)
      +8   drain-pump relay            (0/1)
      +9   two-way valve direction     (0/1)
      +10  indoor fan motor speed      [rpm]
      +13  wired-controller room temp  -> (raw - 3000) / 100  degC

* a **control block** of 4 registers::

      +0   power                       0 = off, 1 = on
      +1   mode << 8 | power bit       mode 0..5
      +2   target temperature          -> 3000 + degC * 100
      +3   fan                         0x0100 = auto, else low byte = gear 1..10

All temperatures use the fixed-point encoding ``raw = 3000 + value * 100``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import modbus


@dataclass(frozen=True)
class UnitDef:
    name: str
    state_base: int
    ctrl_base: int
    sequence: int


# Slot layout of the reference installation: seven slots, one of them empty.
#   state block   = 0x00C8 + 20 * (sequence - 1)
#   control block = 0x03E8 + 15 * (sequence - 1)
# The names must match the names the vendor app/cloud uses, because the Home
# Assistant integration joins the LAN state with the cloud data by name.
# Override the whole map with the ``UNITS_FILE`` environment variable pointing
# at a JSON file like::
#
#     [{"name": "living_room", "sequence": 5},
#      {"name": "bedroom", "sequence": 2, "state_base": "0x00DC"}]
DEFAULT_UNIT_NAMES = ["客厅", "二楼南", "三楼南", "二楼北", "三楼北", "餐厅"]
DEFAULT_UNIT_SEQUENCES = [5, 2, 1, 6, 7, 4]


def _number(value, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError("missing register address")
        return default
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


def _build_units(names: list[str], sequences: list[int]) -> dict[str, UnitDef]:
    units: dict[str, UnitDef] = {}
    for index, (name, sequence) in enumerate(zip(names, sequences)):
        units[name] = UnitDef(
            name=name,
            state_base=0x00C8 + 20 * (sequence - 1),
            ctrl_base=0x03E8 + 15 * (sequence - 1),
            sequence=sequence,
        )
    return units


def _load_units() -> dict[str, UnitDef]:
    """Load the unit map from ``UNITS_FILE`` or fall back to the defaults."""
    path = os.environ.get("UNITS_FILE")
    if not path:
        return _build_units(DEFAULT_UNIT_NAMES, DEFAULT_UNIT_SEQUENCES)
    with open(path, encoding="utf-8") as handle:
        entries = json.load(handle)
    units: dict[str, UnitDef] = {}
    for item in entries:
        name = str(item["name"])
        sequence = int(item.get("sequence") or 0)
        units[name] = UnitDef(
            name=name,
            state_base=_number(item.get("state_base"), 0x00C8 + 20 * (sequence - 1)),
            ctrl_base=_number(item.get("ctrl_base"), 0x03E8 + 15 * (sequence - 1)),
            sequence=sequence,
        )
    return units


UNITS: dict[str, UnitDef] = _load_units()

# Aliases so callers can use pinyin / english keys as well.
ALIASES: dict[str, str] = {
    "canteen": "餐厅", "dining": "餐厅", "dining_room": "餐厅",
    "south3": "三楼南", "third_south": "三楼南",
    "south2": "二楼南", "second_south": "二楼南",
    "north3": "三楼北", "third_north": "三楼北",
    "living": "客厅", "living_room": "客厅",
    "north2": "二楼北", "second_north": "二楼北",
}

MODE_NAMES = {0: "冷风", 1: "送风", 2: "热风", 3: "地暖", 4: "热+暖", 5: "除湿"}
MODE_BY_NAME = {
    "cool": 0, "冷风": 0, "制冷": 0,
    "fan": 1, "送风": 1,
    "heat": 2, "热风": 2, "制热": 2,
    "floor": 3, "地暖": 3,
    "heat_floor": 4, "热+暖": 4, "地暖+热风": 4,
    "dry": 5, "除湿": 5,
}


def resolve(name: str) -> UnitDef:
    """Resolve a unit name / alias to its definition."""
    key = name.strip()
    if key in UNITS:
        return UNITS[key]
    lowered = key.lower().replace(" ", "_").replace("-", "_")
    if lowered in ALIASES:
        return UNITS[ALIASES[lowered]]
    # tolerate names like "san lou nan"
    compact = lowered.replace("_", "")
    for alias, chinese in ALIASES.items():
        if alias.replace("_", "") == compact:
            return UNITS[chinese]
    raise KeyError("unknown unit %r (known: %s)" % (name, ", ".join(UNITS)))


def t(raw: int) -> float:
    """Decode a temperature register."""
    return round((raw - 3000) / 100.0, 2)


def raw_temperature(deg_c: float) -> int:
    """Encode a temperature into its register value."""
    return int(round(3000 + float(deg_c) * 100))


def read_unit(bridge, name: str) -> dict:
    """Read one indoor unit from the DTU (state + control blocks)."""
    unit = resolve(name)
    state = bridge.read_registers(unit.state_base, 14)
    ctrl = bridge.read_registers(unit.ctrl_base, 4)
    power = ctrl[0] & 1
    mode = (ctrl[1] >> 8) & 0xFF
    wind = ctrl[3]
    return {
        "name": unit.name,
        "power": power,
        "power_str": "on" if power else "off",
        "mode": mode,
        "mode_str": MODE_NAMES.get(mode, str(mode)),
        "target_temperature": t(ctrl[2]),
        "return_air_temperature": t(state[5]),
        "room_temperature": t(state[13]),
        "fan_auto": bool(wind & 0x0100),
        "fan_speed": wind & 0xFF,
        "float_switch": state[6],
        "floor_valve": state[7],
        "drain_pump": state[8],
        "valve_direction": state[9],
        "fan_rpm": state[10],
        "firmware": {
            "indoor_board": state[0],
            "display_panel": state[1],
            "code_high": state[2],
            "code_middle": state[3],
            "code_low": state[4],
        },
        "registers": {
            "state_base": unit.state_base,
            "ctrl_base": unit.ctrl_base,
            "ctrl": ctrl,
        },
    }


def read_all(bridge) -> dict[str, dict]:
    """Read every indoor unit.  Returns ``{name: state}``."""
    result: dict[str, dict] = {}
    for name in UNITS:
        result[name] = read_unit(bridge, name)
    return result


def set_target(bridge, name: str, deg_c: float) -> dict:
    """Write the target temperature; no read-back (the poller refreshes soon)."""
    unit = resolve(name)
    bridge.write_register(unit.ctrl_base + 2, raw_temperature(deg_c))
    return {"name": unit.name, "target_temperature": round(float(deg_c), 2)}


def set_power(bridge, name: str, on: bool) -> dict:
    """Switch a unit on/off (one read is needed to keep the mode bits)."""
    unit = resolve(name)
    ctrl = bridge.read_registers(unit.ctrl_base, 4)
    mode = (ctrl[1] >> 8) & 0xFF
    if mode not in MODE_NAMES:
        mode = 2
    bridge.write_register(unit.ctrl_base + 0, 1 if on else 0)
    bridge.write_register(unit.ctrl_base + 1, (mode << 8) | (1 if on else 0))
    return {"name": unit.name, "power": 1 if on else 0}


def set_mode(bridge, name: str, mode: int) -> dict:
    unit = resolve(name)
    if mode not in MODE_NAMES:
        raise ValueError("mode must be 0..5, got %r" % (mode,))
    ctrl = bridge.read_registers(unit.ctrl_base, 4)
    power_bit = ctrl[0] & 1
    bridge.write_register(unit.ctrl_base + 1, (mode << 8) | power_bit)
    return {"name": unit.name, "mode": mode}


def set_fan(bridge, name: str, speed: int | None = None, auto: bool = False) -> dict:
    unit = resolve(name)
    if auto:
        value = 0x0100
    else:
        if speed is None:
            raise ValueError("speed is required when auto is False")
        speed = int(speed)
        if not 1 <= speed <= 10:
            raise ValueError("fan speed must be 1..10, got %r" % (speed,))
        value = speed
    bridge.write_register(unit.ctrl_base + 3, value)
    return {"name": unit.name, "fan_auto": bool(auto), "fan_speed": 0 if auto else value}


def describe(bridge) -> list[dict]:
    """Convenience helper used by the CLI / tests."""
    return [read_unit(bridge, n) for n in UNITS]
