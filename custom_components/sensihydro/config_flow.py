"""Config flow for the SensiHydro integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    SensiHydroAuthError,
    SensiHydroClient,
    SensiHydroConnectionError,
    SensiHydroError,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_SERIALNO,
    CONF_LOCAL_API,
    CONF_NOMINAL_VOLTAGE,
    DEFAULT_LOCAL_API,
    CONF_SCAN_INTERVAL,
    DEFAULT_DEVICE_ID,
    DEFAULT_NOMINAL_VOLTAGE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE = "device"


class SensiHydroConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow of the SensiHydro integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._username: str = ""
        self._password: str = ""
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the cloud credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            client = SensiHydroClient(
                async_get_clientsession(self.hass), username, password
            )
            try:
                devices = await client.async_validate()
            except SensiHydroAuthError:
                errors["base"] = "invalid_auth"
            except SensiHydroConnectionError:
                errors["base"] = "cannot_connect"
            except SensiHydroError as err:
                _LOGGER.error("SensiHydro setup failed: %s", err)
                errors["base"] = "unknown"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during SensiHydro setup")
                errors["base"] = "unknown"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self._username = username
                    self._password = password
                    self._devices = devices
                    if len(devices) == 1:
                        return await self._async_create_entry(devices[0])
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=self._username): str,
                    vol.Required(CONF_PASSWORD, default=self._password or ""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the air conditioner device."""
        if user_input is not None:
            device_id = int(user_input[CONF_DEVICE])
            for device in self._devices:
                if int(device.get("id", 0)) == device_id:
                    return await self._async_create_entry(device)
            return self.async_abort(reason="no_devices")

        choices = {
            str(device.get("id")): f"{device.get('name') or 'Device'} "
            f"({device.get('serialno') or device.get('id')})"
            for device in self._devices
        }
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE): vol.In(choices),
                }
            ),
        )

    async def _async_create_entry(self, device: dict[str, Any]) -> ConfigFlowResult:
        """Create the config entry for a device."""
        device_id = int(device.get("id", DEFAULT_DEVICE_ID))
        await self.async_set_unique_id(f"{DOMAIN}_{device_id}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=str(device.get("name") or f"SensiHydro {device_id}"),
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_DEVICE_ID: device_id,
                CONF_DEVICE_SERIALNO: device.get("serialno") or "",
            },
            options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Reauthenticate when the cloud rejects the account."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new password."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        if user_input is not None:
            client = SensiHydroClient(
                async_get_clientsession(self.hass),
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await client.async_validate()
            except SensiHydroAuthError:
                errors["base"] = "invalid_auth"
            except SensiHydroConnectionError:
                errors["base"] = "cannot_connect"
            except SensiHydroError:
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD, default=""): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: Any,
    ) -> SensiHydroOptionsFlow:
        """Return the options flow."""
        return SensiHydroOptionsFlow()


class SensiHydroOptionsFlow(OptionsFlow):
    """Options of the SensiHydro integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the polling interval."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_NOMINAL_VOLTAGE: float(user_input[CONF_NOMINAL_VOLTAGE]),
                    CONF_LOCAL_API: str(user_input.get(CONF_LOCAL_API, "")).strip(),
                },
            )
        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        current_voltage = self.config_entry.options.get(
            CONF_NOMINAL_VOLTAGE, DEFAULT_NOMINAL_VOLTAGE
        )
        current_local = self.config_entry.options.get(
            CONF_LOCAL_API, DEFAULT_LOCAL_API
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    ),
                    vol.Required(
                        CONF_NOMINAL_VOLTAGE, default=current_voltage
                    ): vol.All(vol.Coerce(float), vol.Range(min=100, max=300)),
                    vol.Optional(CONF_LOCAL_API, default=current_local): str,
                }
            ),
        )
