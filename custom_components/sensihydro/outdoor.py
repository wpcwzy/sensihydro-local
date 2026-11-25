"""Outdoor unit parameters, read directly from the heat-pump controller.

The vendor cloud exposes these as the ``val100``..``val124`` run-time
parameters.  They live in Modbus holding registers ``0x0064``..``0x007C``
(``register = 0x0064 + index``); the mapping below was verified against the
cloud parameter table on real hardware.

Only numeric parameters are patched here.  Textual states (water-flow switch,
relay states, running mode, defrost) keep the cloud wording because their
raw-value -> text mapping is not unambiguous.
"""

from __future__ import annotations

from typing import Any

from .local_api import LocalApiClient, LocalApiError

OUTDOOR_BASE = 0x0064
OUTDOOR_COUNT = 25

#: register index (relative to OUTDOOR_BASE) -> (cloud param code, formatter)
NUMERIC_PARAMS: dict[int, tuple[str, Any]] = {
    0: ("val100", lambda v: f"{(v - 3000) / 100.0:.1f}℃"),   # 环境温度 TA
    1: ("val101", lambda v: f"{(v - 3000) / 100.0:.1f}℃"),   # 制冷水温度 TC
    2: ("val102", lambda v: f"{(v - 3000) / 100.0:.1f}℃"),   # 用户侧出水 TO
    3: ("val103", lambda v: f"{(v - 3000) / 100.0:.1f}℃"),   # 用户侧回水 TI
    4: ("val104", lambda v: f"{(v - 3000) / 100.0:.1f}℃"),   # 用户侧盘管 TC1
    5: ("val105", lambda v: f"{(v - 3000) / 100.0:.1f}℃"),   # 制热出水
    11: ("val111", lambda v: str(v)),                        # 室外电源板版本
    13: ("val113", lambda v: f"{v}V"),                       # AC 输入电压
    14: ("val114", lambda v: f"{v / 10.0:.1f}A"),            # AC 输入电流
    15: ("val115", lambda v: f"{v}P"),                       # 辅路膨胀阀
    16: ("val116", lambda v: f"{v}HZ"),                      # 压缩机转速
    17: ("val117", lambda v: f"{v}P"),                       # 主路膨胀阀
    18: ("val118", lambda v: f"{(v - 3000) / 100.0:.1f}℃"),  # 室外环境 Ta
    19: ("val119", lambda v: f"{(v - 3000) / 100.0:.1f}℃"),  # 室外盘管 Tp
    20: ("val120", lambda v: f"{(v - 3000) / 100.0:.1f}℃"),  # 排气 Td
    21: ("val121", lambda v: f"{(v - 3000) / 100.0:.1f}℃"),  # 回气 Ts
    22: ("val122", lambda v: f"{v / 10.0:.1f}Bar"),          # 蒸发器压力
    23: ("val123", lambda v: f"{v / 10.0:.1f}Bar"),          # 冷凝器压力
}


async def async_read_outdoor(client: LocalApiClient) -> dict[str, str]:
    """Read the outdoor unit registers and format them like the cloud does."""
    values: list[int] = await client.async_read_registers(OUTDOOR_BASE, 20)
    values += await client.async_read_registers(OUTDOOR_BASE + 20, OUTDOOR_COUNT - 20)
    result: dict[str, str] = {}
    for index, (code, formatter) in NUMERIC_PARAMS.items():
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
