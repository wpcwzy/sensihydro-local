"""Async client for the SensiHydro (艾默生 心海悦) cloud API.

The air conditioner gateway (DTU) is a pure cloud TCP client: it has no
local listener, so everything goes through https://sensihydro.copeland.cn.

Authentication is a JWT obtained from ``POST /login`` and passed in the
``x-access-token`` header. The token is valid for ~30 days; every response
carries a freshly issued token, which we keep.

Write requests all go to ``POST /unit/update`` and are distinguished by
``modifyVal``:

    1 -> power      {"status": 0|1}
    2 -> mode       {"mode": "0".."5"}
    3 -> sleep      {"sleep": 0|1}
    4 -> temperature{"flag": 0|1, "targetTemperature": base}  => base +/- 0.5
    5 -> fan speed  {"windSpeed": 0..10, "windModel": 0|1 [, "flag": 0|1]}

The legacy UI uses ``flag`` to step +/- 1 gear / +/- 0.5 C; the newer slider
UI sends the absolute value without ``flag`` for the fan speed. We always
verify the result by reading the unit back and fall back to the stepping
form when the cloud ignores an absolute write.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import (
    API_BASE,
    API_TIMEOUT,
    PARAM_TIMEOUT,
    FAN_AUTO,
    MAX_TEMP,
    MIN_TEMP,
    MODE_FAN,
    MODE_FLOOR,
    MODIFY_MODE,
    MODIFY_POWER,
    MODIFY_SLEEP,
    MODIFY_TEMPERATURE,
    MODIFY_WIND,
)

_LOGGER = logging.getLogger(__name__)

TokenSaver = Callable[[str, int], Awaitable[None]]

READ_ATTEMPTS = 6
READ_DELAY = 1.5


class SensiHydroError(Exception):
    """Base class for SensiHydro errors."""


class SensiHydroAuthError(SensiHydroError):
    """Raised when the cloud rejects the credentials / token."""


class SensiHydroConnectionError(SensiHydroError):
    """Raised when the cloud cannot be reached."""


class SensiHydroApiError(SensiHydroError):
    """Raised when the cloud returns a business error."""

    def __init__(self, status: Any, message: Any = None) -> None:
        """Initialize the error."""
        self.status = status
        self.message = message
        super().__init__(f"cloud error status={status} message={message}")


class SensiHydroWriteError(SensiHydroError):
    """Raised when a write could not be verified on the device."""


def token_expiry(token: str) -> int:
    """Return the ``exp`` claim (unix seconds) of a JWT, or 0."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return int(data.get("exp", 0))
    except (IndexError, ValueError, TypeError):
        return 0


def _as_int(value: Any, default: int = 0) -> int:
    """Best effort int conversion."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    """Best effort float conversion."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SensiHydroClient:
    """Small aiohttp client for the SensiHydro cloud."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        token: str | None = None,
        token_exp: int = 0,
        token_saver: TokenSaver | None = None,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._username = username
        self._password = password
        self._token = token
        self._token_exp = token_exp
        self._token_saver = token_saver
        self._last_saved_exp = token_exp if token else 0
        self._lock = asyncio.Lock()

    @property
    def username(self) -> str:
        """Return the configured account."""
        return self._username

    @property
    def token_expires(self) -> int:
        """Return the current token expiry (unix seconds)."""
        return self._token_exp

    # ------------------------------------------------------------------ HTTP

    async def _async_raw(
        self,
        path: str,
        params: dict[str, Any] | None,
        method: str = "GET",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Perform one HTTP call and decode the JSON envelope."""
        url = f"{API_BASE}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "HomeAssistant-SensiHydro/1.0",
            "Content-Type": "application/json",
        }
        if self._token:
            headers["x-access-token"] = self._token
        try:
            if method == "GET":
                async with self._session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=timeout or API_TIMEOUT,
                ) as resp:
                    status = resp.status
                    text = await resp.text()
            else:
                async with self._session.post(
                    url,
                    json=params or {},
                    headers=headers,
                    timeout=timeout or API_TIMEOUT,
                ) as resp:
                    status = resp.status
                    text = await resp.text()
        except TimeoutError as err:
            raise SensiHydroConnectionError(f"timeout calling {path}") from err
        except aiohttp.ClientError as err:
            raise SensiHydroConnectionError(f"cannot reach cloud: {err}") from err

        if status == 401:
            raise SensiHydroAuthError(f"HTTP 401 for {path}")
        try:
            data = json.loads(text)
        except ValueError as err:
            raise SensiHydroApiError(status, f"invalid JSON: {text[:200]}") from err
        if not isinstance(data, dict):
            raise SensiHydroApiError(status, f"unexpected payload: {text[:200]}")
        return data

    def _async_store_token(self, data: dict[str, Any]) -> None:
        """Remember a newly issued token."""
        token = data.get("token")
        if not isinstance(token, str) or not token or token == self._token:
            return
        self._token = token
        self._token_exp = token_expiry(token) or self._token_exp
        if self._token_saver is None:
            return
        # The cloud re-issues a token on every response; only persist it once a
        # day (or when it is otherwise almost expired) to limit storage churn.
        if self._token_exp - self._last_saved_exp >= 24 * 60 * 60:
            self._last_saved_exp = self._token_exp
            asyncio.create_task(self._token_saver(self._token, self._token_exp))

    async def async_login(self) -> dict[str, Any]:
        """Log in and store the token."""
        _LOGGER.debug("SensiHydro: logging in as %s", self._username)
        data = await self._async_raw(
            "login",
            {"mobilePhone": self._username, "password": self._password},
            "POST",
        )
        if data.get("status") != 0:
            raise SensiHydroAuthError(
                f"login refused: status={data.get('status')} "
                f"message={data.get('message')}"
            )
        token = data.get("token")
        if not token:
            raise SensiHydroAuthError("login returned no token")
        self._token = token
        self._token_exp = token_expiry(token)
        self._last_saved_exp = self._token_exp
        if self._token_saver is not None:
            await self._token_saver(self._token, self._token_exp)
        return data

    async def async_ensure_token(self, margin: int = 6 * 60 * 60) -> None:
        """Make sure a non-expired token is available."""
        async with self._lock:
            if self._token and self._token_exp > time.time() + margin:
                return
            await self.async_login()

    async def async_request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        *,
        retry: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated API call."""
        await self.async_ensure_token()
        data = await self._async_raw(path, params, method, timeout)
        self._async_store_token(data)

        if data.get("status") == 0:
            return data

        message = str(data.get("message"))
        looks_like_auth = any(
            word in message.lower() for word in ("token", "login", "auth", "0008")
        )
        if retry and looks_like_auth:
            # The server side token may have been invalidated before its exp.
            _LOGGER.debug("SensiHydro: token rejected, logging in again")
            await self.async_login()
            return await self.async_request(
                path, params, method, retry=False, timeout=timeout
            )
        if looks_like_auth:
            raise SensiHydroAuthError(f"token rejected: {message}")
        raise SensiHydroApiError(data.get("status"), message)

    # ------------------------------------------------------------- read API

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Return the devices of the account."""
        data = await self.async_request("device/list")
        devices = data.get("data") or []
        return devices if isinstance(devices, list) else []

    async def async_get_units(self, device_id: int) -> list[dict[str, Any]]:
        """Return all indoor units of a device."""
        data = await self.async_request(f"unit/list/{device_id}")
        units = data.get("data") or []
        return units if isinstance(units, list) else []

    async def async_get_unit(
        self, device_id: int, unit_id: int
    ) -> dict[str, Any] | None:
        """Return one indoor unit."""
        for unit in await self.async_get_units(device_id):
            if _as_int(unit.get("id")) == unit_id:
                return unit
        return None

    # ------------------------------------------------------------ write API

    async def async_update_unit(
        self,
        device_id: int,
        device_serialno: str,
        unit_id: int,
        sequence: int,
        payload: dict[str, Any],
    ) -> None:
        """POST one modification for a unit."""
        body = {
            "id": unit_id,
            "deviceId": device_id,
            "sequence": sequence,
            "deviceSerialno": device_serialno,
            **payload,
        }
        _LOGGER.debug("SensiHydro: unit/update %s", body)
        # Never retry a write blindly: a retried relative write (fan gear,
        # temperature base) would be applied twice.
        await self.async_request("unit/update", body, "POST", retry=False)

    async def async_set_host_mode(self, device_id: int, mode_name: str) -> None:
        """Set the outdoor-unit (host) mode through the vendor cloud."""
        from .hostmode import CLOUD_DEVICE_MODE

        value = CLOUD_DEVICE_MODE.get(mode_name)
        if value is None:
            raise SensiHydroError(f"unsupported host mode {mode_name!r}")
        await self.async_request(
            "device/updateMode",
            {"id": device_id, "deviceMode": value},
            "POST",
            retry=False,
        )

    async def _async_wait_for(
        self,
        device_id: int,
        unit_id: int,
        predicate: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any] | None:
        """Read a unit until ``predicate`` is true (or give up)."""
        for attempt in range(READ_ATTEMPTS):
            await asyncio.sleep(READ_DELAY if attempt else 1.0)
            try:
                unit = await self.async_get_unit(device_id, unit_id)
            except SensiHydroError:
                raise
            if unit is not None and predicate(unit):
                return unit
        return None

    async def async_set_power(
        self,
        device_id: int,
        device_serialno: str,
        unit: dict[str, Any],
        turn_on: bool,
    ) -> dict[str, Any]:
        """Switch an indoor unit on or off."""
        unit_id = _as_int(unit.get("id"))
        await self.async_update_unit(
            device_id,
            device_serialno,
            unit_id,
            _as_int(unit.get("sequence")),
            {"modifyVal": MODIFY_POWER, "status": 1 if turn_on else 0},
        )
        expected = 1 if turn_on else 0
        result = await self._async_wait_for(
            device_id, unit_id, lambda u: _as_int(u.get("status")) == expected
        )
        if result is None:
            raise SensiHydroWriteError(
                f"power={expected} was not applied to unit {unit_id}"
            )
        return result

    async def async_set_mode(
        self,
        device_id: int,
        device_serialno: str,
        unit: dict[str, Any],
        mode: int,
    ) -> dict[str, Any]:
        """Set the operation mode (0..5)."""
        unit_id = _as_int(unit.get("id"))
        await self.async_update_unit(
            device_id,
            device_serialno,
            unit_id,
            _as_int(unit.get("sequence")),
            {"modifyVal": MODIFY_MODE, "mode": str(mode)},
        )
        result = await self._async_wait_for(
            device_id, unit_id, lambda u: _as_int(u.get("mode")) == mode
        )
        if result is None:
            raise SensiHydroWriteError(f"mode={mode} was not applied to unit {unit_id}")
        return result

    async def async_set_sleep(
        self,
        device_id: int,
        device_serialno: str,
        unit: dict[str, Any],
        sleep: bool,
    ) -> dict[str, Any]:
        """Enable or disable sleep mode."""
        unit_id = _as_int(unit.get("id"))
        expected = 1 if sleep else 0
        await self.async_update_unit(
            device_id,
            device_serialno,
            unit_id,
            _as_int(unit.get("sequence")),
            {"modifyVal": MODIFY_SLEEP, "sleep": expected},
        )
        result = await self._async_wait_for(
            device_id, unit_id, lambda u: _as_int(u.get("sleep")) == expected
        )
        if result is None:
            raise SensiHydroWriteError(
                f"sleep={expected} was not applied to unit {unit_id}"
            )
        return result

    async def async_set_temperature(
        self,
        device_id: int,
        device_serialno: str,
        unit: dict[str, Any],
        target: float,
    ) -> dict[str, Any]:
        """Set an absolute target temperature (5.0-32.0, 0.5 step).

        The cloud applies ``base +/- 0.5`` (flag 0 = +0.5, flag 1 = -0.5),
        independently of the current device value, so an absolute set is:
        ``flag=0, base=target-0.5`` (or ``flag=1, base=target+0.5`` at 5.0 C).
        """
        unit_id = _as_int(unit.get("id"))
        sequence = _as_int(unit.get("sequence"))
        target = round(min(MAX_TEMP, max(MIN_TEMP, float(target))) * 2) / 2

        def _payload(flag: int, base: float) -> dict[str, Any]:
            return {
                "modifyVal": MODIFY_TEMPERATURE,
                "flag": flag,
                "targetTemperature": round(base, 1),
            }

        if target <= MIN_TEMP:
            attempts = [_payload(1, MIN_TEMP + 0.5), _payload(0, MIN_TEMP + 0.5)]
        else:
            attempts = [
                _payload(0, target - 0.5),
                _payload(1, target + 0.5),
            ]

        for payload in attempts:
            await self.async_update_unit(
                device_id, device_serialno, unit_id, sequence, payload
            )
            result = await self._async_wait_for(
                device_id,
                unit_id,
                lambda u: (_as_float(u.get("targetTemperature")) or 0.0)
                == target,
            )
            if result is not None:
                return result
            _LOGGER.debug(
                "SensiHydro: temperature payload %s did not reach %s C, trying fallback",
                payload,
                target,
            )
        raise SensiHydroWriteError(
            f"target temperature {target} C was not applied to unit {unit_id}"
        )

    async def async_set_fan_mode(
        self,
        device_id: int,
        device_serialno: str,
        unit: dict[str, Any],
        fan_mode: str,
    ) -> dict[str, Any]:
        """Set the fan mode: ``auto`` or a manual gear ``1``-``10``."""
        unit_id = _as_int(unit.get("id"))
        sequence = _as_int(unit.get("sequence"))
        current_speed = _as_int(unit.get("windSpeed"))

        if fan_mode == FAN_AUTO:
            attempts: list[tuple[dict[str, Any], Callable[[dict[str, Any]], bool]]] = [
                (
                    {
                        "modifyVal": MODIFY_WIND,
                        "windSpeed": current_speed if current_speed else 1,
                        "windModel": 1,
                    },
                    lambda u: _as_int(u.get("windModel")) == 1,
                ),
                (
                    {"modifyVal": MODIFY_WIND, "windSpeed": 0, "windModel": 1},
                    lambda u: _as_int(u.get("windModel")) == 1,
                ),
            ]
        else:
            speed = max(1, min(10, _as_int(fan_mode, 1)))
            attempts = [
                (
                    {
                        "modifyVal": MODIFY_WIND,
                        "windSpeed": speed,
                        "windModel": 0,
                    },
                    lambda u, s=speed: _as_int(u.get("windModel")) == 0
                    and _as_int(u.get("windSpeed")) == s,
                )
            ]
            # Legacy stepping write: the cloud applies windSpeed +/- 1.
            if speed > current_speed and speed > 1:
                attempts.append(
                    (
                        {
                            "modifyVal": MODIFY_WIND,
                            "flag": 0,
                            "windSpeed": speed - 1,
                            "windModel": 0,
                        },
                        lambda u, s=speed: _as_int(u.get("windModel")) == 0
                        and _as_int(u.get("windSpeed")) == s,
                    )
                )
            elif speed < current_speed and speed < 10:
                attempts.append(
                    (
                        {
                            "modifyVal": MODIFY_WIND,
                            "flag": 1,
                            "windSpeed": speed + 1,
                            "windModel": 0,
                        },
                        lambda u, s=speed: _as_int(u.get("windModel")) == 0
                        and _as_int(u.get("windSpeed")) == s,
                    )
                )
            else:
                # Same gear, or at a boundary: nudge one gear away first.
                flag = 0 if speed < 10 else 1
                base = speed - 1 if flag == 0 else speed + 1
                attempts.append(
                    (
                        {
                            "modifyVal": MODIFY_WIND,
                            "flag": flag,
                            "windSpeed": base,
                            "windModel": 0,
                        },
                        lambda u, s=speed: _as_int(u.get("windModel")) == 0
                        and _as_int(u.get("windSpeed")) == s,
                    )
                )

        for payload, predicate in attempts:
            await self.async_update_unit(
                device_id, device_serialno, unit_id, sequence, payload
            )
            result = await self._async_wait_for(device_id, unit_id, predicate)
            if result is not None:
                return result
            _LOGGER.debug(
                "SensiHydro: fan payload %s did not reach %s, trying fallback",
                payload,
                fan_mode,
            )
        if _as_int(unit.get("mode")) == MODE_FAN:
            raise SensiHydroWriteError(
                "fan speed cannot be changed while the unit is in fan-only mode"
            )
        raise SensiHydroWriteError(
            f"fan mode {fan_mode} was not applied to unit {unit_id}"
        )

    async def async_check_online(self, device_serialno: str) -> bool:
        """Return whether the DTU gateway is currently connected to the cloud.

        The cloud answers ``device/checkOnline/{serialno}`` with a business
        error ``message.error.0028`` ("device offline") while the gateway is
        not connected, and ``status: 0`` when it is online.
        """
        try:
            data = await self.async_request(
                f"device/checkOnline/{device_serialno}", retry=False
            )
        except SensiHydroApiError:
            return False
        result = data.get("data")
        if isinstance(result, bool):
            return result
        if isinstance(result, (int, float)):
            return bool(result)
        if isinstance(result, dict):
            for key in ("online", "status", "connectFlg"):
                if key in result:
                    return result[key] not in (0, "0", False, None, "")
        return True

    # ------------------------------------------------------- extra read API

    async def async_get_device(self, device_id: int) -> dict[str, Any] | None:
        """Return the outdoor unit / gateway details."""
        data = await self.async_request(f"device/selectById/{device_id}")
        result = data.get("data")
        return result if isinstance(result, dict) else None

    async def async_get_service_config(
        self, device_serialno: str
    ) -> dict[str, Any] | None:
        """Return the full run-time parameter table of the system.

        The cloud exposes ~72 outdoor-unit parameters (temperatures,
        pressures, compressor speed, protections/fault flags, versions) and
        per indoor unit parameters (versions, return-air temperature, valve
        states, fault codes). Values are ``"--"`` while the DTU is offline.
        """
        data = await self.async_request(
            f"customservice/selectConfigById/{device_serialno}",
            timeout=PARAM_TIMEOUT,
        )
        result = data.get("data")
        return result if isinstance(result, dict) else None

    async def async_get_schedules(self, device_id: int) -> list[dict[str, Any]]:
        """Return the timer schedules of a device."""
        data = await self.async_request(
            "unitSchedule/list", {"deviceId": device_id}
        )
        result = data.get("data") or []
        return result if isinstance(result, list) else []

    async def async_get_bind_users(self, device_id: int) -> list[dict[str, Any]]:
        """Return the accounts bound to a device."""
        data = await self.async_request(f"device/bindedUsers/{device_id}")
        result = data.get("data") or []
        return result if isinstance(result, list) else []

    async def async_get_notices(self) -> dict[str, Any]:
        """Return failure / maintenance / repair notifications."""
        notices: dict[str, Any] = {}
        for key, path in (
            ("failure", "notice/selectMessageNotice"),
            ("maintenance", "notice/selectMaintainNotice"),
            ("repair", "notice/selectRepairNotice"),
        ):
            try:
                data = await self.async_request(path, retry=False)
            except SensiHydroApiError as err:
                _LOGGER.debug("SensiHydro: %s not available: %s", path, err)
                continue
            notices[key] = data.get("data")
        return notices

    async def async_get_electric(self) -> Any:
        """Return electricity statistics (may be ``None`` for this device)."""
        data = await self.async_request("electric/list", retry=False)
        return data.get("data")

    # ------------------------------------------------------------ diagnosis

    async def async_validate(self) -> list[dict[str, Any]]:
        """Validate credentials and return the devices."""
        await self.async_login()
        return await self.async_get_devices()


__all__ = [
    "SensiHydroApiError",
    "SensiHydroAuthError",
    "SensiHydroClient",
    "SensiHydroConnectionError",
    "SensiHydroError",
    "SensiHydroWriteError",
    "MODE_FLOOR",
]
