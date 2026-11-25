"""Host (outdoor unit) mode: 常规 / 节能 / 静音 / 强热 and combinations.

Verified on hardware (registers 0x05DC..0x05E0):

    0x05DD  water temperature setpoint: 45 ℃ normally, 50 ℃ in 强热
    0x05DE  静音 flag: 1 = on, 2 = off
    0x05E0  节能 flag: 1 = on, 2 = off

The controller applies the values asynchronously (about 2 s) and the write
order matters: the two flags first, then the setpoint.  The vendor cloud maps
the same registers to ``deviceMode`` 0..8 and picks local changes up on the
next poll, so the official app stays in sync.
"""

from __future__ import annotations

import asyncio

from .local_api import LocalApiClient, LocalApiError

BASE = 0x05DC
COUNT = 5
REG_WATER_SETPOINT = 0x05DD
REG_QUIET = 0x05DE
REG_ECO = 0x05E0
BOOST_SETPOINT = 50
NORMAL_SETPOINT = 45

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

_BITS_TO_NAME = {bits: name for name, bits in MODE_TABLE.items()}


def bits_to_name(quiet: bool, eco: bool, boost: bool) -> str:
    """Return the mode name for a quiet/eco/boost combination."""
    return _BITS_TO_NAME.get((bool(quiet), bool(eco), bool(boost)), "未知")


def parse_registers(values: list[int]) -> dict:
    """Decode the 0x05DC block into a host-mode description."""
    if len(values) < COUNT:
        return {}
    quiet = values[2] == 1
    eco = values[4] == 1
    boost = values[1] == BOOST_SETPOINT
    return {
        "mode": bits_to_name(quiet, eco, boost),
        "quiet": quiet,
        "eco": eco,
        "boost": boost,
        "water_setpoint": values[1],
    }


async def async_read_host_mode(client: LocalApiClient) -> dict:
    """Read the host mode through the local bridge."""
    values = await client.async_read_registers(BASE, COUNT)
    return parse_registers(values)


#: register -> index inside the 0x05DC block
_INDEX = {REG_WATER_SETPOINT: 1, REG_QUIET: 2, REG_ECO: 4}


async def _async_write_verified(
    client: LocalApiClient, addr: int, value: int, tries: int = 3
) -> bool:
    """Write a register and confirm it stuck.

    The controller ignores writes that arrive too close to each other (the
    vendor cloud spaces them ~4-5 seconds apart), so retry after a delay.
    """
    index = _INDEX[addr]
    for _attempt in range(tries):
        await client.async_write_register(addr, value)
        await asyncio.sleep(2.5)
        values = await client.async_read_registers(BASE, COUNT)
        if len(values) > index and values[index] == value:
            return True
    return False


async def async_set_host_mode(client: LocalApiClient, name: str) -> dict:
    """Apply a host mode locally (flags first, setpoint last)."""
    if name not in MODE_TABLE:
        raise LocalApiError(f"unknown host mode {name!r}")
    quiet, eco, boost = MODE_TABLE[name]
    await _async_write_verified(client, REG_QUIET, 1 if quiet else 2)
    await asyncio.sleep(1.0)
    await _async_write_verified(client, REG_ECO, 1 if eco else 2)
    await asyncio.sleep(1.0)
    await _async_write_verified(
        client, REG_WATER_SETPOINT, BOOST_SETPOINT if boost else NORMAL_SETPOINT
    )
    await asyncio.sleep(1.5)
    return await async_read_host_mode(client)
