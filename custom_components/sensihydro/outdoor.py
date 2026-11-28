"""Outdoor unit parameters, read directly from the heat-pump controller.

The vendor cloud exposes these as the ``val100``..``val124`` run-time
parameters.  They live in Modbus holding registers ``0x0064``..``0x007C``
(``register = 0x0064 + index``); the index -> parameter mapping below was
verified against the cloud parameter table on real hardware.

Reading them locally makes the diagnostic entities independent of the vendor
cloud, which is slow, returns ``"--"`` while it has no fresh data and sometimes
fails entirely.  Only the fault/protection flags (``val125_*`` ..) have no known
register and still come from the cloud table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the module importable
    from .local_api import LocalApiClient

OUTDOOR_BASE = 0x0064
OUTDOOR_COUNT = 25

#: Raw run mode -> text.  Learned values (cloud wording) take precedence.
_MODE_TEXT: dict[int, str] = {
    0: "停机模式",
    1: "制冷模式",
    2: "制热模式",
    3: "地暖模式",
    4: "制热+地暖模式",
    5: "除湿模式",
}
_learned_modes: dict[int, str] = {}


def learn_run_mode(raw: int, text: str | None) -> None:
    """Remember the cloud wording for a raw run-mode value."""
    if not text or text == "--":
        return
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return
    if _learned_modes.get(value) != text:
        _learned_modes[value] = text


def run_mode_text(raw: int, fallback: str | None = None) -> str:
    """Return the text for a raw run-mode value."""
    if fallback and fallback != "--":
        return fallback
    return _learned_modes.get(raw) or _MODE_TEXT.get(raw) or f"模式{raw}"


def _number(value: float) -> str:
    """Format a number the way the cloud does (``21``, not ``21.0``)."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _temp(value: int) -> str:
    """Encode a fixed-point temperature (``raw = 3000 + value * 100``)."""
    return f"{_number((value - 3000) / 100.0)}℃"


def _on_off(value: int) -> str:
    return "ON" if value else "OFF"


def _voltage(value: int) -> str:
    # The board reports tenths of a volt (0 while it does not measure).
    return f"{_number(value / 10.0)}V"


def _current(value: int) -> str:
    return f"{_number(value / 10.0)}A"


def _pressure(value: int) -> str:
    return f"{_number(value / 10.0)}Bar"


def _water_flow(value: int) -> str:
    # Verified against the cloud: the register is 1 while the switch is OPEN.
    return "水流开关断开" if value else "水流开关闭合"


def _defrost(value: int) -> str:
    return "除霜中" if value else "未除霜"


#: register index (relative to OUTDOOR_BASE) -> (cloud code, formatter)
PARAM_FORMATTERS: dict[int, tuple[str, Callable[[int], str]]] = {
    0: ("val100", _temp),
    1: ("val101", _temp),
    2: ("val102", _temp),
    3: ("val103", _temp),
    4: ("val104", _temp),
    5: ("val105", _temp),
    6: ("val106", _water_flow),
    7: ("val107", _on_off),
    8: ("val108", _on_off),
    9: ("val109", _on_off),
    10: ("val110", _on_off),
    11: ("val111", lambda v: str(v)),
    12: ("val112", lambda v: run_mode_text(v)),
    13: ("val113", _voltage),
    14: ("val114", _current),
    15: ("val115", lambda v: f"{v}P"),
    16: ("val116", lambda v: f"{v}HZ"),
    17: ("val117", lambda v: f"{v}P"),
    18: ("val118", _temp),
    19: ("val119", _temp),
    20: ("val120", _temp),
    21: ("val121", _temp),
    22: ("val122", _pressure),
    23: ("val123", _pressure),
    24: ("val124", _defrost),
}


async def async_read_outdoor(client: LocalApiClient) -> dict[str, str]:
    """Read the outdoor unit registers and format them like the cloud does."""
    values: list[int] = []
    # The DTU accepts at most 20 registers per request.
    for offset in range(0, OUTDOOR_COUNT, 20):
        count = min(20, OUTDOOR_COUNT - offset)
        values += await client.async_read_registers(OUTDOOR_BASE + offset, count)
    result: dict[str, str] = {}
    for index, (code, formatter) in PARAM_FORMATTERS.items():
        if index >= len(values):
            continue
        try:
            result[code] = formatter(values[index])
        except (TypeError, ValueError):
            continue
    return result


def apply(device_params: dict[str, dict[str, Any]], values: dict[str, str]) -> None:
    """Overwrite the cloud values with the locally read ones."""
    for code, value in values.items():
        param = device_params.get(code)
        if isinstance(param, dict):
            param["value"] = value
