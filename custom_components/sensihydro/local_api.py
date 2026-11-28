"""Client for the local SensiHydro bridge (docker container on the LAN).

The bridge relays the DTU connection to the vendor cloud (so the official app
keeps working) and exposes a small JSON API that talks Modbus/TCP directly to
the heat-pump controller.  Reads are real-time and writes are applied locally
without a round-trip through the vendor cloud.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import LOCAL_API_TIMEOUT

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=LOCAL_API_TIMEOUT)


class LocalApiError(Exception):
    """Raised when the local bridge cannot be reached or returns an error."""


class LocalApiClient:
    """Thin async wrapper around the bridge REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout or DEFAULT_TIMEOUT

    # ------------------------------------------------------------------ basics

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            async with self._session.request(
                method, url, json=payload, timeout=self._timeout
            ) as response:
                if response.status >= 500:
                    raise LocalApiError(f"bridge HTTP {response.status} for {path}")
                data = await response.json(content_type=None)
        except aiohttp.ClientError as err:
            raise LocalApiError(f"cannot reach local bridge: {err}") from err
        except asyncio.TimeoutError as err:
            raise LocalApiError(f"local bridge timeout for {path}") from err
        except ValueError as err:
            raise LocalApiError(f"invalid JSON from local bridge: {err}") from err
        if not isinstance(data, dict):
            raise LocalApiError(f"unexpected local bridge answer for {path}")
        if not data.get("ok"):
            raise LocalApiError(str(data.get("error") or "local bridge error"))
        return data

    # -------------------------------------------------------------------- read

    async def async_status(self) -> dict[str, Any]:
        """Return the bridge health status."""
        return await self._request("GET", "/api/status")

    async def async_get_state(
        self, max_age: float | None = None
    ) -> dict[str, dict[str, Any]]:
        """Return the state of every indoor unit keyed by unit name.

        ``max_age`` asks the bridge to refresh its snapshot synchronously when
        it is older than that many seconds, so a 15 s poll cycle costs exactly
        one Modbus sweep (the bridge otherwise refreshes in the background).
        """
        path = "/api/state"
        if max_age is not None:
            path += f"?max_age={max_age:g}"
        data = await self._request("GET", path)
        units = data.get("units") or {}
        if not isinstance(units, dict):
            raise LocalApiError("bridge returned no unit table")
        return units

    async def async_read_registers(self, addr: int, count: int) -> list[int]:
        """Read raw holding registers through the bridge (Modbus injection)."""
        data = await self._request(
            "GET", f"/api/read?addr=0x{addr:04x}&count={int(count)}"
        )
        values = data.get("values")
        if not isinstance(values, list):
            raise LocalApiError(f"bridge returned no registers for 0x{addr:04x}")
        return [int(v) for v in values]

    async def async_write_register(self, addr: int, value: int) -> None:
        """Write one raw holding register through the bridge."""
        await self._request(
            "POST",
            "/api/write",
            {"addr": f"0x{addr:04x}", "value": f"0x{int(value) & 0xFFFF:04x}"},
        )

    async def async_get_unit(self, name: str) -> dict[str, Any]:
        """Return one indoor unit state."""
        data = await self._request("GET", f"/api/unit/{_quote(name)}")
        return data.get("unit") or {}

    # ------------------------------------------------------------------- write

    async def async_set_target(self, name: str, temperature: float) -> dict[str, Any]:
        """Set the target temperature locally."""
        data = await self._request(
            "POST", f"/api/unit/{_quote(name)}/target", {"temperature": temperature}
        )
        return data.get("unit") or {}

    async def async_set_power(self, name: str, on: bool) -> dict[str, Any]:
        """Switch an indoor unit on or off locally."""
        data = await self._request(
            "POST", f"/api/unit/{_quote(name)}/power", {"on": bool(on)}
        )
        return data.get("unit") or {}

    async def async_set_mode(self, name: str, mode: int) -> dict[str, Any]:
        """Set the operation mode locally."""
        data = await self._request(
            "POST", f"/api/unit/{_quote(name)}/mode", {"mode": int(mode)}
        )
        return data.get("unit") or {}

    async def async_set_fan(
        self, name: str, speed: int | None = None, auto: bool = False
    ) -> dict[str, Any]:
        """Set the fan speed locally (``auto`` ignores ``speed``)."""
        payload = {"auto": True} if auto else {"speed": int(speed or 1)}
        data = await self._request(
            "POST", f"/api/unit/{_quote(name)}/fan", payload
        )
        return data.get("unit") or {}


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
