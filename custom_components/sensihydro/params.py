"""Static definition of the system's run-time parameter table.

Why this module exists
----------------------
The vendor cloud exposes the run-time parameters through
``customservice/selectConfigById`` as a table of ``code -> {name, value}``
entries.  That endpoint is slow (10-40 s), occasionally returns ``"--"`` and
sometimes fails completely.  The Home Assistant integration used to create its
diagnostic entities *from that table*, so a single failed call during a restart
made 97 entities disappear ("unavailable") until the integration was reloaded.

The set of parameters is fixed, therefore it is defined here.  Entities are
created from these definitions regardless of the cloud, and their values are
filled from the controller itself over Modbus (see ``outdoor.py`` and
``coordinator.py``); the cloud table is only a fallback for the few fields that
have no known register.
"""

from __future__ import annotations

from typing import Any

#: Outdoor-unit / gateway parameters: ``code -> display name``.
#: The names must match the vendor cloud exactly because the sensor metadata
#: (unit, device class, precision) is derived from the Chinese wording.
DEVICE_PARAM_NAMES: dict[str, str] = {
    "val100": "热泵外机环境温度(TA)",
    "val101": "制冷水温度(TC)",
    "val102": "用户侧出水温度(TO)",
    "val103": "用户侧回水温度(TI)",
    "val104": "用户侧盘管温度1(TC1)",
    "val105": "制热出水温度",
    "val106": "水流开关",
    "val107": "制冷水路切换阀继电器状态",
    "val108": "循环水泵控制继电器状态",
    "val109": "制热运行状态",
    "val110": "制冷运行状态",
    "val111": "室外电源板程序版本",
    "val112": "当前运行模式",
    "val113": "AC输入电压(RMS)",
    "val114": "AC输入电流（RMS）",
    "val115": "辅路电子膨胀阀运行开度",
    "val116": "压缩机运行转速",
    "val117": "主路电子膨胀阀运行开度",
    "val118": "室外环境温度(Ta)",
    "val119": "室外盘管温度(Tp)",
    "val120": "室外排气温度(Td)",
    "val121": "室外回气温度(Ts)",
    "val122": "蒸发器压力",
    "val123": "冷凝器压力",
    "val124": "进入除霜信号",
}

#: Per indoor-unit parameters: ``code -> display name``.
UNIT_PARAM_NAMES: dict[str, str] = {
    "val200": "内机控制板软件版本",
    "val201": "显示面板软件版本",
    "val202": "内机识别码高位",
    "val203": "内机识别码中位",
    "val204": "内机识别码低位",
    "val205": "内机回风温度",
    "val206": "浮球开关信号",
    "val207": "地暖热阀继电器开关",
    "val208": "排水泵电源继电器开关",
    "val209": "二通阀方向",
    "val210": "内机电机转速",
    "val213": "线控器环境温度",
}


def _defs(names: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Build a ``code -> parameter`` table with empty values."""
    return {
        code: {"code": code, "name": name, "value": None}
        for code, name in names.items()
    }


def device_param_defs() -> dict[str, dict[str, Any]]:
    """Return a fresh copy of the outdoor-unit parameter table."""
    return _defs(DEVICE_PARAM_NAMES)


def unit_param_defs() -> dict[str, dict[str, Any]]:
    """Return a fresh copy of the indoor-unit parameter table."""
    return _defs(UNIT_PARAM_NAMES)
