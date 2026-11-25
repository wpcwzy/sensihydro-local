"""Small helpers shared by the SensiHydro platforms."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Parameter codes of the cloud run-time table which represent 0/1 status or
# protection/fault flags (as opposed to temperatures, pressures, speeds...).
FLAG_CODE_PREFIXES: tuple[str, ...] = (
    "val125_",
    "val126_",
    "val127_",
    "val128_",
    "val129_",
    "val130_",
    "val131_",
    "val211_",
    "val212_",
)

# The cloud appends a unit to the raw parameter string, e.g. ``9.9℃``,
# ``21.6Bar``, ``89HZ``, ``22.5A``, ``0V``, ``0转/分``.
_UNIT_ALIASES: dict[str, str | None] = {
    "℃": "°C",
    "°C": "°C",
    "HZ": "Hz",
    "Hz": "Hz",
    "hz": "Hz",
    "Bar": "bar",
    "BAR": "bar",
    "bar": "bar",
    "A": "A",
    "V": "V",
    "%": "%",
    "转/分": "r/min",
    "P": None,  # pulse/step position, unitless
}

_NUMBER_WITH_UNIT = re.compile(r"^(-?\d+(?:\.\d+)?)\s*([^\d\s].*)$")

_TRUE_STRINGS = {"ON", "TRUE", "YES", "1", "闭合", "开", "告警", "故障", "异常", "保护"}
_FALSE_STRINGS = {
    "告警无",
    "正常",
    "无",
    "无告警",
    "OFF",
    "FALSE",
    "NO",
    "0",
    "断开",
    "关",
    "未除霜",
    "未运行",
    "停止",
}


def is_flag_code(code: str) -> bool:
    """Return True if the parameter is a boolean status/fault flag."""
    return code.startswith(FLAG_CODE_PREFIXES)


def _clean_number(number: float) -> float | int:
    """Return an int when the float has no fractional part."""
    return int(number) if float(number).is_integer() else number


@dataclass(slots=True, frozen=True)
class ParsedValue:
    """A parsed cloud parameter value."""

    value: float | int | str | None = None
    unit: str | None = None


def parse_param(raw: object) -> ParsedValue:
    """Parse a cloud parameter value.

    The value can be numeric (``"25"``), numeric with a cloud unit
    (``"9.9℃"``, ``"21.6Bar"``, ``"0转/分"``) or a textual state
    (``"ON"``, ``"告警无"``, ``"制热模式"``).
    """
    if raw is None:
        return ParsedValue()
    text = str(raw).strip()
    if text in ("", "--", "null", "None", "nan"):
        return ParsedValue()

    try:
        return ParsedValue(_clean_number(float(text)))
    except ValueError:
        pass

    match = _NUMBER_WITH_UNIT.match(text)
    if match:
        try:
            number = float(match.group(1))
        except ValueError:
            return ParsedValue(text)
        suffix = match.group(2).strip()
        unit = _UNIT_ALIASES.get(suffix)
        if unit is None and suffix not in _UNIT_ALIASES:
            # Unknown unit suffix: keep the number, drop the suffix.
            return ParsedValue(_clean_number(number))
        return ParsedValue(_clean_number(number), unit)

    return ParsedValue(text)


def parse_param_value(raw: object) -> float | int | str | None:
    """Return the parsed value without the unit."""
    return parse_param(raw).value


def is_truthy(raw: object) -> bool:
    """Interpret a 0/1 (or ON/OFF/闭合) flag value."""
    parsed = parse_param_value(raw)
    if parsed is None:
        return False
    if isinstance(parsed, (int, float)):
        return parsed != 0
    text = str(parsed).strip()
    upper = text.upper()
    if upper in _FALSE_STRINGS or text in _FALSE_STRINGS:
        return False
    if upper in _TRUE_STRINGS or text in _TRUE_STRINGS:
        return True
    # Unknown textual state of a fault flag: treat as active (usually it is
    # the name of the triggered protection / fault).
    return True
