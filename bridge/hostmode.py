"""Host (outdoor unit) mode: 常规 / 节能 / 静音 / 强热 and their combinations.

Registers (verified on real hardware, 0x05DC block):

    +0x01 = 0x05DD  water temperature setpoint: 45 ℃ normally, 50 ℃ in 强热
    +0x02 = 0x05DE  静音 flag: 1 = on, 2 = off
    +0x04 = 0x05E0  节能 flag: 1 = on, 2 = off

The controller applies the values asynchronously (about 2 seconds) and the
write order matters: write the two flags first, then the setpoint.
The vendor cloud maps the same registers to ``deviceMode`` 0..8 and picks the
change up on its next poll, so the official app stays in sync.
"""

from __future__ import annotations

import time

REG_WATER_SETPOINT = 0x05DD
REG_QUIET = 0x05DE
REG_ECO = 0x05E0

BASE = 0x05DC
COUNT = 5

BOOST_SETPOINT = 50
NORMAL_SETPOINT = 45

#: mode name -> (quiet, eco, boost)
MODE_TABLE: dict[str, tuple[bool, bool, bool]] = {
    "常规": (False, False, False),
    "节能": (False, True, False),
    "静音": (True, False, False),
    "节能+静音": (True, True, False),
    "强热": (False, False, True),
    "强热+节能": (False, True, True),
    "强热+静音": (True, False, True),
    "强热+节能+静音": (True, True, True),
}

#: (quiet, eco, boost) -> mode name
_BITS_TO_NAME = {bits: name for name, bits in MODE_TABLE.items()}

#: mode name -> vendor cloud ``deviceMode`` value
CLOUD_DEVICE_MODE: dict[str, str] = {
    "常规": "0",
    "节能": "1",
    "节能+静音": "2",
    "静音": "3",
    "强热": "5",
    "强热+节能": "6",
    "强热+静音": "7",
    "强热+节能+静音": "8",
}

MODE_ORDER: list[str] = [
    "常规",
    "节能",
    "静音",
    "节能+静音",
    "强热",
    "强热+节能",
    "强热+静音",
    "强热+节能+静音",
]

ALIASES: dict[str, str] = {
    "normal": "常规", "off": "常规",
    "eco": "节能", "energy": "节能",
    "quiet": "静音", "silent": "静音", "mute": "静音",
    "boost": "强热", "strong": "强热",
}


def resolve(name: str) -> str:
    """Resolve an English alias / case variant to a canonical mode name."""
    key = str(name).strip()
    if key in MODE_TABLE:
        return key
    lowered = key.lower()
    if lowered in ALIASES:
        return ALIASES[lowered]
    for canonical in MODE_TABLE:
        if canonical.lower() == lowered:
            return canonical
    raise KeyError("unknown host mode %r (known: %s)" % (name, ", ".join(MODE_ORDER)))


def bits_to_name(quiet: bool, eco: bool, boost: bool) -> str:
    return _BITS_TO_NAME.get((bool(quiet), bool(eco), bool(boost)), "未知")


def read_host_mode(bridge) -> dict:
    """Read the current host mode from the controller."""
    values = bridge.read_registers(BASE, COUNT)
    quiet = values[2] == 1
    eco = values[4] == 1
    boost = values[1] == BOOST_SETPOINT
    return {
        "mode": bits_to_name(quiet, eco, boost),
        "quiet": quiet,
        "eco": eco,
        "boost": boost,
        "water_setpoint": values[1],
        "registers": {
            "0x05dd": values[1],
            "0x05de": values[2],
            "0x05e0": values[4],
        },
    }


_INDEX = {REG_WATER_SETPOINT: 1, REG_QUIET: 2, REG_ECO: 4}


def _write_verified(bridge, addr: int, value: int, tries: int = 3) -> bool:
    """Write a register and confirm it stuck (the controller drops close writes)."""
    index = _INDEX[addr]
    for _attempt in range(tries):
        bridge.write_register(addr, value)
        time.sleep(2.5)
        values = bridge.read_registers(BASE, COUNT)
        if len(values) > index and values[index] == value:
            return True
    return False


def write_host_mode(bridge, quiet: bool, eco: bool, boost: bool) -> dict:
    """Apply a host mode; flags first, setpoint last (order matters)."""
    _write_verified(bridge, REG_QUIET, 1 if quiet else 2)
    time.sleep(1.0)
    _write_verified(bridge, REG_ECO, 1 if eco else 2)
    time.sleep(1.0)
    _write_verified(
        bridge, REG_WATER_SETPOINT, BOOST_SETPOINT if boost else NORMAL_SETPOINT
    )
    time.sleep(1.5)
    return read_host_mode(bridge)


def set_host_mode(bridge, name: str) -> dict:
    """Set a host mode by its (canonical) name."""
    quiet, eco, boost = MODE_TABLE[resolve(name)]
    return write_host_mode(bridge, quiet, eco, boost)
