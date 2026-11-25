"""Set up the SensiHydro (艾默生 心海悦) cloud integration."""

from __future__ import annotations

import logging
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import SensiHydroClient
from .const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_API,
    DEFAULT_LOCAL_API,
    CONF_DEVICE_SERIALNO,
    CONF_NOMINAL_VOLTAGE,
    CONF_SCAN_INTERVAL,
    DEFAULT_NOMINAL_VOLTAGE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .coordinator import SensiHydroConfigEntry, SensiHydroCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: SensiHydroConfigEntry) -> bool:
    """Set up SensiHydro from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    username: str = entry.data[CONF_USERNAME]
    password: str = entry.data[CONF_PASSWORD]
    device_id: int = entry.data[CONF_DEVICE_ID]
    device_serialno: str = entry.data.get(CONF_DEVICE_SERIALNO, "")
    scan_interval: int = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    nominal_voltage: float = float(
        entry.options.get(CONF_NOMINAL_VOLTAGE, DEFAULT_NOMINAL_VOLTAGE)
    )
    local_api_url: str = str(
        entry.options.get(CONF_LOCAL_API, DEFAULT_LOCAL_API) or ""
    ).strip()

    store: Store[dict] = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
    stored = await store.async_load() or {}

    async def _save_token(token: str, token_exp: int) -> None:
        await store.async_save({"token": token, "token_exp": token_exp})

    client = SensiHydroClient(
        async_get_clientsession(hass),
        username,
        password,
        token=stored.get("token"),
        token_exp=int(stored.get("token_exp") or 0),
        token_saver=_save_token,
    )

    device_name = entry.title or f"SensiHydro {device_id}"
    # Register the outdoor unit / gateway as a device of its own so the
    # cloud connectivity sensor and all indoor units are grouped below it.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(device_id))},
        name=device_name,
        manufacturer=MANUFACTURER,
        model=MODEL,
    )

    coordinator = SensiHydroCoordinator(
        hass,
        entry,
        client,
        device_id,
        device_serialno,
        scan_interval,
        device_name,
        nominal_voltage,
        local_api_url=local_api_url,
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SensiHydroConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(
    hass: HomeAssistant, entry: SensiHydroConfigEntry
) -> None:
    """Reload the entry when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)
